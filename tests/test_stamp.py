"""scripts/stamp.py: a release names itself, everything else names its commit."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stamp", ROOT / "scripts" / "stamp.py")
stamp = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
assert spec and spec.loader
spec.loader.exec_module(stamp)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.3.0"\n')
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_dev_hash_then_release_tag_then_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    short = _git(repo, "rev-parse", "--short=7", "HEAD")
    assert stamp.build_stamp(repo, env={}) == f"0.3.0-dev-{short}"
    _git(repo, "tag", "dev-2026-09-12")  # dev tags never name a release
    assert stamp.build_stamp(repo, env={}) == f"0.3.0-dev-{short}"
    _git(repo, "tag", "v0.3.0")
    assert stamp.build_stamp(repo, env={}) == "0.3.0"
    (repo / "notes.txt").write_text("uncommitted\n")  # untracked file = dirty tree
    assert stamp.build_stamp(repo, env={}) == f"0.3.0-dev-{short}-dirty"


def test_override_and_no_git(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert stamp.build_stamp(repo, env={"SPLITTER_RELEASE_VERSION": "v1.2.3"}) == "1.2.3"
    bare = tmp_path / "tarball"
    bare.mkdir()
    (bare / "pyproject.toml").write_text('version = "0.9.0-alpha.1"\n')
    assert stamp.build_stamp(bare, env={}) == "0.9.0-alpha.1"


def test_write_stamp(tmp_path: Path) -> None:
    out = tmp_path / "_stamp.py"
    stamp.write_stamp("0.3.0-dev-abc1234", out)
    ns: dict[str, str] = {}
    exec(out.read_text(), ns)
    assert ns["STAMP"] == "0.3.0-dev-abc1234"
