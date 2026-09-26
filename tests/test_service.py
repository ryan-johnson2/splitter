"""``splitter service``: the rendered service definitions per platform (dry run)."""

from __future__ import annotations

from pathlib import Path

import pytest

from splitter import service


def target(platform: str) -> service.Target:
    return service.Target(
        ["/opt/splitter/venv/bin/python", "-m", "splitter"], Path("/var/lib/splitter"), platform
    )


def test_systemd_unit_runs_this_install_from_boot() -> None:
    unit = service.systemd_unit(target("linux"))
    assert "ExecStart=/opt/splitter/venv/bin/python -m splitter" in unit
    assert "Environment=SPLITTER_DATA_DIR=/var/lib/splitter" in unit
    assert "WantedBy=multi-user.target" in unit and "Restart=always" in unit
    steps = service.plan(target("linux"), "install")
    assert steps[0][0] == "write" and str(steps[0][1]).startswith(
        "/etc/systemd/system/splitter.service"
    )
    assert ("run", ["systemctl", "enable", "--now", "splitter.service"]) in steps
    assert ("run", ["systemctl", "stop", "splitter.service"]) in service.plan(
        target("linux"), "stop"
    )


def test_launchd_plist_and_windows_commands() -> None:
    plist = service.launchd_plist(target("darwin"))
    assert "<string>fpv.splitter</string>" in plist and "<key>KeepAlive</key><true/>" in plist
    win = service.Target(
        [r"C:\Program Files\Splitter\splitter-sidecar.exe"],
        Path(r"C:\ProgramData\Splitter"),
        "win32",
    )
    cmds = service.windows_commands(win, "install")
    create = cmds[0]
    assert create[:3] == ["sc", "create", "Splitter"] and "--service" in create[4]
    assert '"C:\\Program Files\\Splitter\\splitter-sidecar.exe"' in create[4]
    assert any(c[:2] == ["sc", "sdset"] and "IU" in c[3] for c in cmds)  # users may start/stop
    assert service.windows_commands(win, "remove")[-1] == ["sc", "delete", "Splitter"]


def test_dry_run_prints_and_touches_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    code = service.main(
        [
            "install",
            "--platform",
            "linux",
            "--data-dir",
            "/tmp/x",
            "--exe",
            "py -m splitter",
            "--dry-run",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "would write /etc/systemd/system/splitter.service" in out
    assert "would run: systemctl enable --now splitter.service" in out
    assert "ExecStart=py -m splitter" in out
