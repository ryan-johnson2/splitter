"""The node side of sync: push finished runs up, pull reference bundles down.

One supervised task next to the game bridge. Runs are queued in the ``outbox``
table at race end (``RaceController`` calls :meth:`Uploader.enqueue`), uploaded
oldest first with backoff, and acknowledged by the web with the reference
bundle for the run's PB key, which is cached and handed to the controller.
Nothing here is ever required for a run to record; with no upstream
configured the task just waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import ssl
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import truststore

from splitter.core.rundoc import DOC_VERSION
from splitter.db import repos
from splitter.db.models import Race
from splitter.db.runtime_settings import RuntimeSettings
from splitter.live.hub import LiveHub
from splitter.util import utcnow
from splitter.version import __version__

if TYPE_CHECKING:
    from splitter.game.controller import RaceController

log = logging.getLogger(__name__)

ClientFactory = Callable[[str], httpx.AsyncClient]

BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
IDLE_S = 60.0

# ``Uploader.web``: the web's answer at last contact. ``unknown`` until the first
# exchange (or after the settings change), then one of the rest. Anything but
# ``ok`` paints the header indicator red with the reason.
WEB_STATES = ("unknown", "ok", "receiving_off", "bad_token", "old_web", "unreachable")


def token_allowed(url: str) -> bool:
    """Only send the token over https, or over http to a private/LAN address."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return "." not in host  # a bare LAN hostname


def _default_client(url: str) -> httpx.AsyncClient:
    # Verify against the machine's own trust store (Windows CryptoAPI, macOS
    # Keychain, the OpenSSL store on Linux) rather than certifi's public bundle.
    # A self-hosted web signed by a home CA is the normal case here, and a root
    # the user has trusted on the machine must be enough — no CA file to point
    # at, nothing extra in the frozen build.
    verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(30.0, connect=10.0), verify=verify)


class Uploader:
    def __init__(
        self,
        settings: RuntimeSettings,
        session_factory: Any,
        hub: LiveHub,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._hub = hub
        self._client_factory = client_factory or _default_client
        self._client: httpx.AsyncClient | None = None
        self._client_url = ""
        self._wake = asyncio.Event()
        # One pass at a time: the loop and a manual "Send now" must never both
        # push the same run (the web would see two inserts in flight).
        self._pass_lock = asyncio.Lock()
        self._ref_requests: set[repos.PBKey] = set()
        self.controller: RaceController | None = None
        self.pending = 0
        self.terminal = 0
        self.last_error = ""
        self.last_ack_at: str | None = None
        # What the web said the last time we spoke to it (``WEB_STATES``). The
        # header's *Web* indicator is painted from this, not from "nothing is
        # pending": a node with no runs to send never used to contact the web
        # at all, so its indicator read green while the web had receiving off.
        self.web = "unknown"
        self.web_checked_at: str | None = None

    # ── configuration / status ─────────────────────────────────────

    @property
    def url(self) -> str:
        return self._settings.get("upstream_url").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.url and self._settings.get("upstream_token").strip())

    @property
    def token_blocked(self) -> bool:
        return self.configured and not token_allowed(self.url)

    def status(self) -> dict[str, Any]:
        return {
            "upstream": self.configured,
            "url": self.url,
            "pending": self.pending,
            "terminal": self.terminal,
            "last_ack_at": self.last_ack_at,
            "last_error": self.last_error,
            "token_blocked": self.token_blocked,
            "keep_local": self._settings.get_bool("keep_local_runs"),
            "web": self.web,
            "web_checked_at": self.web_checked_at,
        }

    # ── what the web said ──────────────────────────────────────────

    def _web_ok(self) -> None:
        self.web = "ok"
        self.last_error = ""
        self.web_checked_at = utcnow().replace(microsecond=0).isoformat() + "Z"

    def _web_failed(self, error: str, status: int | None = None) -> None:
        """Record a failed exchange. 401/403 are the web's own answers (bad token,
        receiving off); anything else means it could not be reached or is broken."""
        if status == 403:
            self.web = "receiving_off"
            self.last_error = "HTTP 403: the web is not receiving runs (turn it on in its Settings)"
        elif status == 401:
            self.web = "bad_token"
            self.last_error = (
                "HTTP 401: the web refused the token (copy it again from its Settings)"
            )
        else:
            self.web = "unreachable"
            self.last_error = error
        self.web_checked_at = utcnow().replace(microsecond=0).isoformat() + "Z"

    def wake(self) -> None:
        self._wake.set()

    async def reconfigure(self) -> None:
        """Settings changed: drop the client so the next call uses the new URL."""
        if self._client is not None and self._client_url != self.url:
            await self._client.aclose()
            self._client = None
        self.last_error = ""
        self.web = "unknown"
        self.web_checked_at = None
        async with self._sf() as db:
            await repos.reset_outbox_backoff(db)
        if self.configured:
            await self.enqueue_all()
        await self.refresh_counts()
        self.wake()

    async def enqueue_all(self) -> int:
        """Queue every kept run that has never been queued (older than the
        upstream setting, or hand-imported). Returns how many."""
        async with self._sf() as db:
            ids = await repos.unqueued_runs(db)
        for race_id in ids:
            await self.enqueue(race_id)
        if ids:
            log.info("queued %d earlier runs for upload", len(ids))
        return len(ids)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client_url != self.url:
            self._client = self._client_factory(self.url)
            self._client_url = self.url
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.get('upstream_token').strip()}",
            "X-Splitter-Node": self._settings.get("node_name")[:80],
            "X-Splitter-Node-Id": self._settings.get("node_id"),
            "User-Agent": f"splitter/{__version__}",
        }

    def _broadcast(self) -> None:
        self._hub.broadcast("sync", self.status())

    async def refresh_counts(self) -> None:
        async with self._sf() as db:
            self.pending, self.terminal, acked = await repos.outbox_counts(db)
        if acked is not None:
            self.last_ack_at = acked.replace(microsecond=0).isoformat() + "Z"

    # ── queueing ───────────────────────────────────────────────────

    async def enqueue(self, race_id: int) -> int | None:
        """Queue a finished/aborted-with-crossings run; assigns its ``seq``.

        Called for every kept run whether or not an upstream is configured,
        so runs recorded before the upstream was set are pushed once it is.
        """
        async with self._sf() as db:
            race = await db.get(Race, race_id)
            if race is None or not race.uuid:
                return None
            seq = self._settings.get_int("node_seq") + 1
            await self._settings.set(db, "node_seq", str(seq))
            race.seq = seq
            await repos.enqueue_outbox(db, race.uuid, seq)
            await db.commit()
        await self.refresh_counts()
        self._broadcast()
        self.wake()
        return seq

    def request_reference(self, key: repos.PBKey) -> None:
        """Ask the web for this key's bundle when the cache has none (cold start)."""
        if self.configured and key.valid:
            self._ref_requests.add(key)
            self.wake()

    # ── the loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        await self.refresh_counts()
        while True:
            delay = IDLE_S
            if self.configured and not self.token_blocked:
                try:
                    delay = await self.process_once()
                except Exception:  # never let a bug stop the bridge's sibling
                    log.exception("uploader pass failed")
                    delay = BACKOFF_MIN_S
            elif self.token_blocked:
                self.last_error = "refusing to send the token over plain http to a public address"
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    async def process_once(self) -> float:
        """One pass: pending reference requests, then every due outbox row.
        Returns how long to sleep before the next pass."""
        async with self._pass_lock:
            return await self._pass()

    async def _pass(self) -> float:
        for key in list(self._ref_requests):
            self._ref_requests.discard(key)
            await self.fetch_reference(key)
        now = utcnow()
        async with self._sf() as db:
            due = await repos.due_outbox(db, now)
        for row_uuid in due:
            await self._upload(row_uuid)
        if not due:
            # Nothing to push, so nothing would have told us how the web is.
            # Ask it: one small GET per idle pass (every IDLE_S, and at once
            # after the settings change) keeps the indicator honest.
            await self.check()
        await self.refresh_counts()
        async with self._sf() as db:
            next_at = await repos.next_outbox_at(db)
        if next_at is None:
            return IDLE_S
        return max(0.5, min(IDLE_S, (next_at - utcnow()).total_seconds()))

    async def _upload(self, race_uuid: str) -> None:
        async with self._sf() as db:
            race = await repos.get_race_by_uuid(db, race_uuid)
            if race is None:
                # Deleted locally before it was sent: nothing to push.
                await repos.finish_outbox(db, race_uuid, terminal="gone", error="run deleted here")
                return
            doc = await repos.export_run(db, race, __version__)
            race_id = race.id
        try:
            r = await self._http().put(
                f"/api/ingest/runs/{race_uuid}", json=doc, headers=self._headers()
            )
        except httpx.HTTPError as e:
            await self._fail(race_uuid, f"{type(e).__name__}: {e}")
            return
        if r.status_code == 200:
            body = r.json()
            async with self._sf() as db:
                await repos.finish_outbox(db, race_uuid, acked=True)
            self._web_ok()
            log.info("run %s pushed to %s (%s)", race_uuid[:8], self.url, body.get("status"))
            if body.get("reference"):
                await self.apply_bundle(body["reference"])
            if not self._settings.get_bool("keep_local_runs"):
                async with self._sf() as db:
                    await repos.delete_race(db, race_id, tombstone=False)
                if self.controller is not None:
                    await self.controller.refresh_reference()
            return
        detail = _detail(r)
        if r.status_code == 409:
            await self._terminal(race_uuid, "version", detail)
        elif r.status_code == 410:
            await self._terminal(race_uuid, "deleted", detail)
        elif r.status_code == 422:
            await self._terminal(race_uuid, "rejected", detail)
        else:
            await self._fail(race_uuid, f"HTTP {r.status_code}: {detail}", r.status_code)

    async def _fail(self, race_uuid: str, error: str, status: int | None = None) -> None:
        async with self._sf() as db:
            attempts = await repos.fail_outbox(db, race_uuid, error, _backoff)
        self._web_failed(error, status)
        log.warning("push of %s failed (attempt %d): %s", race_uuid[:8], attempts, error)
        self._broadcast()

    async def _terminal(self, race_uuid: str, reason: str, detail: str) -> None:
        async with self._sf() as db:
            await repos.finish_outbox(db, race_uuid, terminal=reason, error=detail)
        self.last_error = f"{reason}: {detail}"
        log.error("push of %s will not be retried (%s): %s", race_uuid[:8], reason, detail)
        self._broadcast()

    # ── references ─────────────────────────────────────────────────

    async def fetch_reference(self, key: repos.PBKey) -> dict[str, Any] | None:
        try:
            r = await self._http().get(
                "/api/reference",
                params={
                    "track_id": key.track_id,
                    "quad_model_id": key.quad_model_id,
                    "race_laps": key.race_laps,
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            self._web_failed(f"{type(e).__name__}: {e}")
            self._broadcast()
            return None
        if r.status_code != 200:
            self._web_failed(f"HTTP {r.status_code}: {_detail(r)}", r.status_code)
            self._broadcast()
            return None
        self._web_ok()
        payload = r.json()
        await self.apply_bundle(payload)
        return dict(payload)

    async def apply_bundle(self, payload: dict[str, Any]) -> None:
        k = payload.get("key") or {}
        key = repos.PBKey(
            int(k.get("track_id", 0)), int(k.get("quad_model_id", 0)), int(k.get("race_laps", 0))
        )
        if not key.valid:
            return
        async with self._sf() as db:
            await repos.cache_put(db, key, payload)
        if self.controller is not None:
            await self.controller.on_bundle(key)

    # ── one-off actions from the Settings page ─────────────────────

    async def ping(self) -> dict[str, Any]:
        """*Test connection*: ask the web who it is. Also the heartbeat behind the
        header indicator (:meth:`check`), so every outcome updates ``web``."""
        if not self.configured:
            return {"ok": False, "error": "no upstream configured"}
        if self.token_blocked:
            return {"ok": False, "error": "plain http to a public address: use https"}
        try:
            r = await self._http().get("/api/ingest/ping", headers=self._headers())
        except httpx.HTTPError as e:
            self._web_failed(f"{type(e).__name__}: {e}")
            self._broadcast()
            return {"ok": False, "error": self.last_error}
        if r.status_code != 200:
            self._web_failed(f"HTTP {r.status_code}: {_detail(r)}", r.status_code)
            self._broadcast()
            return {"ok": False, "error": self.last_error}
        body = r.json()
        ok = int(body.get("doc_version", 0)) >= DOC_VERSION
        if ok:
            self._web_ok()
        else:
            self.web = "old_web"
            self.last_error = (
                f"the web understands document version {body.get('doc_version')}; "
                f"this node writes {DOC_VERSION}. Upgrade the web."
            )
            self.web_checked_at = utcnow().replace(microsecond=0).isoformat() + "Z"
        self._broadcast()
        return {
            "ok": ok,
            "version": body.get("version"),
            "doc_version": body.get("doc_version"),
            "name": body.get("name", ""),
            "error": "" if ok else self.last_error,
        }

    async def check(self) -> None:
        """The idle heartbeat: a ping whose result only goes to ``web``."""
        if self.configured and not self.token_blocked:
            await self.ping()

    async def purge(self) -> int:
        """Delete every local run the web has acknowledged."""
        async with self._sf() as db:
            n = await repos.purge_acked(db)
        if self.controller is not None:
            await self.controller.refresh_reference()
        return n

    async def proxy_search(self, q: str, source: str, limit: int) -> dict[str, Any]:
        """The online track picker through the web (the private client lives there)."""
        r = await self._http().get(
            "/api/tracks/search", params={"q": q, "source": source, "limit": limit}
        )
        r.raise_for_status()
        return dict(r.json())


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_MAX_S, BACKOFF_MIN_S * (2 ** max(0, attempts - 1))))


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)[:200]
    return str(body)[:200]
