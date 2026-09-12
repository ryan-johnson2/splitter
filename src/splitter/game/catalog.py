"""Track catalogue: search VelociDrone's official and community track lists.

Single player never tells Splitter which track is loaded, so the tablet's
"Track…" dialog has to be told. Rather than free text, it searches the
game's own online lists (the same endpoints Marshal's event form uses) so a
pick carries the canonical name plus the online track and scene ids.

Both endpoints answer without credentials (verified 2026-09-12 against the
live API). The client comes from the **private** ``velocidrone-tracks``
package (or the full ``velocidrone-api`` when that is installed instead);
neither ships in this repository, so the import is optional: without one the
picker is disabled and the manual-id entry is the fallback. Release builds
compile ``velocidrone-tracks`` in. Calls are synchronous, so they go through
``asyncio.to_thread``; the official list is cached whole (it is ~2000 rows and
changes rarely), community searches are cached per query for a few minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from splitter.core.scenes import scene_name

log = logging.getLogger(__name__)

SOURCE_OFFICIAL = "official"
SOURCE_COMMUNITY = "community"
SOURCES = (SOURCE_OFFICIAL, SOURCE_COMMUNITY)

# Official-track ``type`` codes (from Marshal's track_sources; inferred from
# the 1.16 list). Community rows carry the label as a string already.
OFFICIAL_TRACK_TYPES: dict[int, str] = {
    1: "Beginner",
    2: "Intermediate",
    3: "Advanced",
    4: "X Class",
    5: "Large class",
    6: "Mega",
    7: "Whoop",
    8: "Micro",
    9: "Micro",
    10: "Combat",
    11: "Freestyle",
    12: "Street League",
    13: "Pro Spec",
}


class CatalogError(Exception):
    """The online track list could not be fetched."""


UNAVAILABLE = "online track lists are not included in this build — enter the track id manually"


@dataclass(frozen=True)
class TrackRef:
    source: str  # official | community
    track_id: int
    scene_id: int
    name: str
    kind: str = ""  # Beginner / Intermediate / Advanced / Freestyle / ...
    author: str = ""  # community tracks only

    @property
    def scenery(self) -> str:
        return scene_name(self.scene_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scenery"] = self.scenery
        return d


@dataclass
class SearchResult:
    tracks: list[TrackRef] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # source → message

    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracks": [t.to_dict() for t in self.tracks],
            "errors": dict(self.errors),
            "available": self.available,
        }


class OfficialRow(Protocol):
    id: int
    name: str
    scene_id: int
    track_type: int


class CommunityRow(Protocol):
    id: int
    track_name: str
    track_type: str
    playername: str
    scenery_id: int


class TrackAPI(Protocol):
    """The two calls we need (velocidrone-tracks, velocidrone-api, or a fake in tests)."""

    def get_official_tracks(self) -> list[Any]: ...
    def search_tracks(self, query: Any = None) -> list[Any]: ...


QueryFactory = Callable[[str], Any]


def load_backend() -> tuple[TrackAPI, QueryFactory] | None:
    """The installed online client, if any: velocidrone-tracks first, else velocidrone-api."""
    try:
        from velocidrone_tracks import TrackClient
        from velocidrone_tracks import UserTrackQuery as TQ

        return TrackClient(timeout=15.0), lambda name: TQ(track_name=name, order_by_rating=True)
    except ImportError as exc:
        log.info("velocidrone_tracks not importable: %s", exc)
    try:
        from velocidrone_api import UserTrackQuery as AQ
        from velocidrone_api import VelociDroneAPI

        api = VelociDroneAPI(email="", hardware_key="", timeout=15.0)
        return api, lambda name: AQ(track_name=name, order_by_rating=True)
    except ImportError as exc:
        log.info("velocidrone_api not importable: %s", exc)
        return None


def _official_ref(t: OfficialRow) -> TrackRef:
    return TrackRef(
        SOURCE_OFFICIAL,
        t.id,
        t.scene_id,
        t.name.strip(),
        OFFICIAL_TRACK_TYPES.get(t.track_type, f"type {t.track_type}"),
    )


def _community_ref(t: CommunityRow) -> TrackRef:
    return TrackRef(
        SOURCE_COMMUNITY, t.id, t.scenery_id, t.track_name.strip(), t.track_type, t.playername
    )


def rank(tracks: list[TrackRef], query: str) -> list[TrackRef]:
    """Exact name first, then prefix matches, then the rest; alphabetical within each."""
    q = query.strip().lower()

    def key(t: TrackRef) -> tuple[int, str]:
        n = t.name.lower()
        tier = 0 if n == q else 1 if n.startswith(q) else 2 if q in n else 3
        return (tier, n)

    return sorted(tracks, key=key)


class TrackCatalog:
    def __init__(
        self,
        api: TrackAPI | None = None,
        *,
        query_factory: QueryFactory | None = None,
        official_ttl: float = 3600.0,
        search_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # No credentials: neither endpoint needs them (see module docstring).
        self._api: TrackAPI | None = api
        self._query: QueryFactory = query_factory or (lambda name: name)
        if api is None:
            backend = load_backend()
            if backend is not None:
                self._api, self._query = backend
            else:
                log.warning(
                    "no online track client installed (velocidrone-tracks) — "
                    "track search disabled, manual track ids only"
                )
        self._official_ttl = official_ttl
        self._search_ttl = search_ttl
        self._clock = clock
        self._official: list[TrackRef] = []
        self._official_at: float | None = None
        self._official_lock = asyncio.Lock()
        self._community: dict[str, tuple[float, list[TrackRef]]] = {}

    @property
    def available(self) -> bool:
        """False when no online client is installed (public source builds)."""
        return self._api is not None

    # ── official ─────────────────────────────────────────────────────

    async def official_tracks(self) -> list[TrackRef]:
        """The whole official list, fetched at most once per TTL."""
        if self._api is None:
            raise CatalogError(UNAVAILABLE)
        async with self._official_lock:
            now = self._clock()
            if self._official_at is not None and now - self._official_at < self._official_ttl:
                return self._official
            try:
                rows = await asyncio.to_thread(self._api.get_official_tracks)
            except Exception as exc:
                if self._official:  # stale beats nothing
                    log.warning("official track list refresh failed, using cached: %s", exc)
                    return self._official
                raise CatalogError(f"official track list: {exc}") from exc
            self._official = [_official_ref(t) for t in rows if t.name.strip()]
            self._official_at = now
            log.info("official track list loaded: %d tracks", len(self._official))
            return self._official

    async def search_official(self, query: str) -> list[TrackRef]:
        q = query.strip().lower()
        if not q:
            return []
        return rank([t for t in await self.official_tracks() if q in t.name.lower()], q)

    # ── community ────────────────────────────────────────────────────

    async def search_community(self, query: str) -> list[TrackRef]:
        q = query.strip()
        if not q:
            return []
        now = self._clock()
        cached = self._community.get(q.lower())
        if cached is not None and now - cached[0] < self._search_ttl:
            return cached[1]
        if self._api is None:
            raise CatalogError(UNAVAILABLE)
        try:
            rows = await asyncio.to_thread(self._api.search_tracks, self._query(q))
        except Exception as exc:
            raise CatalogError(f"community track search: {exc}") from exc
        found = rank([_community_ref(t) for t in rows if t.track_name.strip()], q)
        self._community[q.lower()] = (now, found)
        if len(self._community) > 200:
            oldest = min(self._community, key=lambda k: self._community[k][0])
            del self._community[oldest]
        return found

    async def resolve(self, name: str) -> TrackRef | None:
        """Exact-name match for a track the game named (hosted-room ``session``).

        Official first, then community; ``None`` when nothing matches exactly
        or the lists are unreachable. Used to give game-sourced sessions an id.
        """
        wanted = name.strip().lower()
        if not wanted:
            return None
        res = await self.search(name, limit=200)
        exact = [t for t in res.tracks if t.name.strip().lower() == wanted]
        exact.sort(key=lambda t: (t.source != SOURCE_OFFICIAL, t.track_id))
        return exact[0] if exact else None

    # ── combined ─────────────────────────────────────────────────────

    async def search(self, query: str, source: str = "", limit: int = 30) -> SearchResult:
        """Search one source, or both concurrently when ``source`` is empty/``all``.

        A failing source is reported in ``errors`` rather than failing the
        whole search, so the tablet still gets whatever came back.
        """
        result = SearchResult(available=self.available)
        if not query.strip():
            return result
        if self._api is None:
            result.errors["online"] = UNAVAILABLE
            return result
        wanted = [source] if source in SOURCES else list(SOURCES)
        calls = {
            SOURCE_OFFICIAL: self.search_official,
            SOURCE_COMMUNITY: self.search_community,
        }
        outcomes = await asyncio.gather(*(calls[s](query) for s in wanted), return_exceptions=True)
        for s, outcome in zip(wanted, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                log.warning("track search (%s) failed: %s", s, outcome)
                result.errors[s] = str(outcome)
            else:
                result.tracks.extend(outcome)
        # Interleave by rank so an exact community hit is not buried under
        # every official track containing the word.
        result.tracks = rank(result.tracks, query)[:limit]
        return result
