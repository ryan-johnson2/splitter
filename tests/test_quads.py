"""Bundled quad/scene catalog: loading, grouping, lookups, settings.db extraction."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from splitter.core import quads
from splitter.core.quads import (
    CatalogFileError,
    GameCatalog,
    QuadModel,
    catalog_from_json,
    catalog_to_json,
    load_bundled_catalog,
    read_game_db,
)


def test_bundled_catalog_loads() -> None:
    cat = load_bundled_catalog()
    assert len(cat.models) > 100 and len(cat.scenes) > 40
    assert cat.scenes[8] == "Football Stadium"
    names = [c["class_name"] for c in cat.classes()]
    assert names[:3] == ["Racing", "Mega", "Micro"]
    assert all(m["class_id"] == c["class_id"] for c in cat.classes() for m in c["models"])


def test_lookups() -> None:
    cat = GameCatalog(
        models=[
            QuadModel(5, "Source One", 1),
            QuadModel(7, "Baby Ape", 3),
            QuadModel(9, "Retired", 1, active=False),
        ],
        scenes={8: "Football Stadium"},
    )
    assert cat.model(7) is not None and cat.model(7).name == "Baby Ape"  # type: ignore[union-attr]
    assert cat.model_by_name(" baby ape ") == cat.model(7)
    assert cat.model_by_name("") is None and cat.model_by_name("nope") is None
    groups = cat.classes()
    assert [(g["class_id"], g["class_name"], len(g["models"])) for g in groups] == [
        (1, "Racing", 1),
        (3, "Micro", 1),
    ]  # inactive models are hidden
    assert cat.model(5).to_dict() == {  # type: ignore[union-attr]
        "model_id": 5,
        "name": "Source One",
        "class_id": 1,
        "class_name": "Racing",
    }


def test_registry_and_names() -> None:
    saved = quads.catalog()
    try:
        quads.use_catalog(GameCatalog(models=[QuadModel(5, "Source One", 1)], scenes={8: "Stad"}))
        assert quads.model_name(5) == "Source One" and quads.model_name(0) == ""
        assert quads.scene_name(8) == "Stad"
        assert quads.scene_name(16) == "Empty Scene Day"  # seed fallback
        assert quads.scene_name(999) == "Scene 999"
        assert quads.class_name(6) == "Freestyle" and quads.class_name(0) == ""
        assert quads.class_name(42) == "Class 42"
    finally:
        quads.use_catalog(saved)


def test_json_roundtrip() -> None:
    cat = GameCatalog(
        models=[QuadModel(1, "A", 2, False)], scenes={3: "X"}, disabled_scenes=[4], source="s"
    )
    assert catalog_from_json(catalog_to_json(cat)) == cat
    with pytest.raises(CatalogFileError):
        catalog_from_json("{nope")


def test_read_game_db(tmp_path: Path) -> None:
    db = tmp_path / "settings.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE models (
            model_id INT, name TEXT, prefab_name TEXT, component_group_id INT, active TEXT);
        INSERT INTO models VALUES (5, 'Source One', 'p', 1, 'true'), (9, 'Old', 'p', 1, 'false');
        CREATE TABLE sceneries (id INT, name TEXT, title TEXT, type TEXT, enabled TEXT);
        INSERT INTO sceneries VALUES (8, 'stadium', 'Football Stadium', 'track', 'true'),
                                     (1, 'MainMenu', 'Main Menu', 'menu', 'true'),
                                     (99, 'gone', 'Gone', 'track', 'false');
        """
    )
    conn.commit()
    conn.close()
    cat = read_game_db(db)
    assert [(m.model_id, m.active) for m in cat.models] == [(9, False), (5, True)]
    assert cat.scenes == {8: "Football Stadium"} and cat.disabled_scenes == [99]
    assert cat.source == "settings.db" and cat.generated_at
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(CatalogFileError):
        read_game_db(bad)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("CREATE TABLE t (x)")
    with pytest.raises(CatalogFileError, match="models"):
        read_game_db(empty)
