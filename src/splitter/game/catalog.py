"""Track catalogue: search VelociDrone's official and community track lists.

Single player never tells Splitter which track is loaded, so the tablet's
"Track…" dialog has to be told. Rather than free text, it searches the
game's own online lists (the same endpoints Marshal's event form uses) so a
pick carries the canonical name plus the online track and scene ids.

Both endpoints answer without credentials (verified 2026-09-12 against the
live API): ``get_official_tracks`` is a plain GET, and ``rated_tracks_list``
accepts empty auth fields. The client is the synchronous ``velocidrone-api``
library, so calls go through ``asyncio.to_thread``; the official list is
cached whole (it is ~2000 rows and changes rarely), community searches are
cached per query for a few minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from velocidrone_api import UserTrackQuery, VelociDroneAPI
from velocidrone_api.models import OfficialTrack, TrackListEntry

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

    def to_dict(self) -> dict[str, Any]:
        return {"tracks": [t.to_dict() for t in self.tracks], "errors": dict(self.errors)}


class TrackAPI(Protocol):
    """The two calls we need from ``VelociDroneAPI`` (swapped for a fake in tests)."""

    def get_official_tracks(self) -> list[OfficialTrack]: ...
    def search_tracks(self, query: UserTrackQuery | None = None) -> list[TrackListEntry]: ...


def _official_ref(t: OfficialTrack) -> TrackRef:
    return TrackRef(
        SOURCE_OFFICIAL,
        t.id,
        t.scene_id,
        t.name.strip(),
        OFFICIAL_TRACK_TYPES.get(t.track_type, f"type {t.track_type}"),
    )


def _community_ref(t: TrackListEntry) -> TrackRef:
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
        official_ttl: float = 3600.0,
        search_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # No credentials: neither endpoint needs them (see module docstring).
        self._api: TrackAPI = api or VelociDroneAPI(email="", hardware_key="", timeout=15.0)
        self._official_ttl = official_ttl
        self._search_ttl = search_ttl
        self._clock = clock
        self._official: list[TrackRef] = []
        self._official_at: float | None = None
        self._official_lock = asyncio.Lock()
        self._community: dict[str, tuple[float, list[TrackRef]]] = {}

    # ── official ─────────────────────────────────────────────────────

    async def official_tracks(self) -> list[TrackRef]:
        """The whole official list, fetched at most once per TTL."""
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
        try:
            rows = await asyncio.to_thread(
                self._api.search_tracks, UserTrackQuery(track_name=q, order_by_rating=True)
            )
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
        result = SearchResult()
        if not query.strip():
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
