"""TrackCatalog over a fake API: ranking, caching, and partial failure."""

from __future__ import annotations

from typing import Any

import pytest
from velocidrone_api import UserTrackQuery
from velocidrone_api.models import OfficialTrack, TrackListEntry

from splitter.game.catalog import CatalogError, TrackCatalog, TrackRef, rank


class FakeAPI:
    def __init__(self) -> None:
        self.official_calls = 0
        self.search_calls: list[str] = []
        self.official_fail = False
        self.search_fail = False

    def get_official_tracks(self) -> list[OfficialTrack]:
        self.official_calls += 1
        if self.official_fail:
            raise RuntimeError("offline")
        return [
            OfficialTrack(id=1902, name="USADT Recruitment", scene_id=16, track_type=2),
            OfficialTrack(id=1, name="Gemfan", scene_id=12, track_type=1),
            OfficialTrack(
                id=2053, name="USADT Recruitment Competition V2", scene_id=8, track_type=2
            ),
        ]

    def search_tracks(self, query: UserTrackQuery | None = None) -> list[TrackListEntry]:
        assert query is not None
        self.search_calls.append(query.track_name)
        if self.search_fail:
            raise RuntimeError("offline")
        return [
            TrackListEntry(
                id=40001,
                track_name="USADT Champs Trial 01",
                track_type="Intermediate",
                playername="reconfpv_",
                scenery_id=16,
            ),
            TrackListEntry(
                id=32868,
                track_name="USADT v2",
                track_type="Beginner",
                playername="ForzaFPV",
                scenery_id=8,
            ),
        ]


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def catalog(api: FakeAPI) -> TrackCatalog:
    clock = {"t": 0.0}
    cat = TrackCatalog(api, official_ttl=100, search_ttl=10, clock=lambda: clock["t"])
    cat.clock = clock  # type: ignore[attr-defined]
    return cat


async def test_search_both_sources_ranked(catalog: TrackCatalog) -> None:
    res = await catalog.search("usadt")
    names = [t.name for t in res.tracks]
    assert names == [
        "USADT Champs Trial 01",
        "USADT Recruitment",
        "USADT Recruitment Competition V2",
        "USADT v2",
    ]
    assert res.errors == {}
    first = res.tracks[0]
    assert first.source == "community" and first.track_id == 40001 and first.scene_id == 16
    assert first.scenery == "Empty Scene Day" and first.author == "reconfpv_"
    d = first.to_dict()
    assert d["scenery"] == "Empty Scene Day" and d["kind"] == "Intermediate"


async def test_exact_match_outranks_prefix() -> None:
    rows = [
        TrackRef("official", 2, 1, "Gemfan Pro"),
        TrackRef("official", 1, 1, "Gemfan"),
        TrackRef("community", 3, 1, "Big Gemfan"),
    ]
    assert [t.name for t in rank(rows, "gemfan")] == ["Gemfan", "Gemfan Pro", "Big Gemfan"]


async def test_single_source_and_empty_query(catalog: TrackCatalog, api: FakeAPI) -> None:
    assert (await catalog.search("")).tracks == []
    off = await catalog.search("gemfan", "official")
    assert [t.track_id for t in off.tracks] == [1] and api.search_calls == []
    com = await catalog.search("usadt", "community")
    assert all(t.source == "community" for t in com.tracks) and api.official_calls == 1


async def test_caches(catalog: TrackCatalog, api: FakeAPI) -> None:
    await catalog.search("usadt")
    await catalog.search("USADT")
    await catalog.search("gemfan")
    assert api.official_calls == 1  # whole list cached
    assert api.search_calls == ["usadt", "gemfan"]  # case-insensitive query cache
    catalog.clock["t"] = 50  # type: ignore[attr-defined]
    await catalog.search("usadt")
    assert api.search_calls == ["usadt", "gemfan", "usadt"]  # search TTL expired
    assert api.official_calls == 1  # official TTL not yet
    catalog.clock["t"] = 200  # type: ignore[attr-defined]
    await catalog.search("usadt", "official")
    assert api.official_calls == 2


async def test_partial_failure_keeps_other_source(catalog: TrackCatalog, api: FakeAPI) -> None:
    api.search_fail = True
    res = await catalog.search("usadt")
    assert [t.source for t in res.tracks] == ["official", "official"]
    assert "community" in res.errors and "offline" in res.errors["community"]


async def test_official_failure_uses_stale_then_errors(catalog: TrackCatalog, api: FakeAPI) -> None:
    await catalog.official_tracks()
    api.official_fail = True
    catalog.clock["t"] = 500  # type: ignore[attr-defined]
    assert len(await catalog.official_tracks()) == 3  # stale copy served
    fresh = TrackCatalog(api)
    with pytest.raises(CatalogError):
        await fresh.official_tracks()


async def test_limit(catalog: TrackCatalog) -> None:
    res = await catalog.search("usadt", limit=2)
    assert len(res.tracks) == 2


def test_track_ref_scenery_unknown() -> None:
    t: Any = TrackRef("official", 1, 999, "X")
    assert t.scenery == "Scene 999"


async def test_resolve_exact_name_prefers_official(catalog: TrackCatalog, api: FakeAPI) -> None:
    ref = await catalog.resolve("usadt recruitment")
    assert ref is not None and ref.source == "official" and ref.track_id == 1902
    ref = await catalog.resolve("USADT Champs Trial 01")
    assert ref is not None and ref.source == "community" and ref.track_id == 40001
    assert await catalog.resolve("USADT") is None  # substring is not a match
    assert await catalog.resolve("  ") is None
    api.search_fail = True
    api.official_fail = True
    assert await catalog.resolve("Nothing Loads") is None
