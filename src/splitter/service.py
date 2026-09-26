"""``splitter service``: run this install from boot as a system service.

The node is only useful while the game is running, which on a gaming PC means
"since boot, without anyone starting it" (docs/sync-design.md). One command
renders and registers the platform's service definition for *this*
executable and data directory:

- Linux: a systemd system unit (``/etc/systemd/system/splitter.service``).
- macOS: a launchd daemon (``/Library/LaunchDaemons/fpv.splitter.plist``).
- Windows: ``sc create`` for the sidecar in service mode
  (``splitter-sidecar --service``, which needs ``pywin32``), with a DACL that
  lets interactive users start and stop it without elevation.

``--dry-run`` prints what would be written and run; tests use it, and so can
anyone who wants to review before registering. Registration itself needs
root / an elevated prompt.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NAME = "Splitter"
UNIT_NAME = "splitter.service"
LAUNCHD_LABEL = "fpv.splitter"

# Default service DACL (SYSTEM, Administrators, Interactive users, Services) plus
# start/stop/pause (RP, WP, DT) for interactive users, so the desktop window can
# start the service without a UAC prompt.
WINDOWS_SDDL = (
    "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
    "(A;;CCLCSWRPWPDTLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)"
)


@dataclass(frozen=True)
class Target:
    exe: list[str]  # the command that runs the server
    data_dir: Path
    platform: str  # linux | darwin | win32


def default_exe() -> list[str]:
    """What runs the server: the frozen sidecar, or this interpreter with ``-m splitter``."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "splitter"]


def default_data_dir(platform: str) -> Path:
    if platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / NAME
    if platform == "darwin":
        return Path("/Library/Application Support") / NAME
    return Path("/var/lib/splitter")


def systemd_unit(t: Target) -> str:
    cmd = " ".join(shlex.quote(c) for c in t.exe)
    if getattr(sys, "frozen", False):
        cmd += f" --data-dir {shlex.quote(str(t.data_dir))} --host 0.0.0.0 --port 8100"
    return f"""[Unit]
Description={NAME} — VelociDrone lap timer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=SPLITTER_DATA_DIR={t.data_dir}
Environment=HOST=0.0.0.0
Environment=PORT=8100
WorkingDirectory={t.data_dir}
ExecStart={cmd}
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


def launchd_plist(t: Target) -> str:
    args = list(t.exe)
    if getattr(sys, "frozen", False):
        args += ["--data-dir", str(t.data_dir), "--host", "0.0.0.0", "--port", "8100"]
    items = "".join(f"\n      <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{items}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SPLITTER_DATA_DIR</key><string>{t.data_dir}</string>
    <key>HOST</key><string>0.0.0.0</string>
    <key>PORT</key><string>8100</string>
  </dict>
  <key>WorkingDirectory</key><string>{t.data_dir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{t.data_dir}/splitter-service.log</string>
  <key>StandardErrorPath</key><string>{t.data_dir}/splitter-service.log</string>
</dict>
</plist>
"""


def windows_commands(t: Target, action: str) -> list[list[str]]:
    """The ``sc`` invocations for each action (the service runs the sidecar in
    service mode, see ``desktop_entry.py``)."""
    if action == "install":
        bin_path = " ".join(
            [f'"{c}"' if " " in c else c for c in t.exe]
            + ["--service", "--data-dir", f'"{t.data_dir}"']
        )
        return [
            [
                "sc",
                "create",
                NAME,
                "binPath=",
                bin_path,
                "start=",
                "auto",
                "DisplayName=",
                f"{NAME} lap timer",
            ],
            ["sc", "description", NAME, "VelociDrone lap timer: records every run from boot."],
            ["sc", "sdset", NAME, WINDOWS_SDDL],
            ["sc", "failure", NAME, "reset=", "86400", "actions=", "restart/5000"],
            ["sc", "start", NAME],
        ]
    if action == "remove":
        return [["sc", "stop", NAME], ["sc", "delete", NAME]]
    if action in ("start", "stop"):
        return [["sc", action, NAME]]
    return [["sc", "query", NAME]]


def plan(t: Target, action: str) -> list[tuple[str, str | list[str]]]:
    """(kind, payload) steps: ``("write", path + text)`` or ``("run", argv)``."""
    steps: list[tuple[str, str | list[str]]] = []
    if t.platform == "win32":
        for argv in windows_commands(t, action):
            steps.append(("run", argv))
        return steps
    if t.platform == "darwin":
        path = f"/Library/LaunchDaemons/{LAUNCHD_LABEL}.plist"
        if action == "install":
            steps.append(("write", f"{path}\n{launchd_plist(t)}"))
            steps.append(("run", ["launchctl", "bootstrap", "system", path]))
        elif action == "remove":
            steps.append(("run", ["launchctl", "bootout", "system", path]))
            steps.append(("run", ["rm", "-f", path]))
        elif action in ("start", "stop"):
            verb = "kickstart" if action == "start" else "kill"
            args = ["launchctl", verb] + (["-k"] if action == "start" else ["SIGTERM"])
            steps.append(("run", [*args, f"system/{LAUNCHD_LABEL}"]))
        else:
            steps.append(("run", ["launchctl", "print", f"system/{LAUNCHD_LABEL}"]))
        return steps
    path = f"/etc/systemd/system/{UNIT_NAME}"
    if action == "install":
        steps.append(("write", f"{path}\n{systemd_unit(t)}"))
        steps.append(("run", ["systemctl", "daemon-reload"]))
        steps.append(("run", ["systemctl", "enable", "--now", UNIT_NAME]))
    elif action == "remove":
        steps.append(("run", ["systemctl", "disable", "--now", UNIT_NAME]))
        steps.append(("run", ["rm", "-f", path]))
        steps.append(("run", ["systemctl", "daemon-reload"]))
    elif action in ("start", "stop"):
        steps.append(("run", ["systemctl", action, UNIT_NAME]))
    else:
        steps.append(("run", ["systemctl", "--no-pager", "status", UNIT_NAME]))
    return steps


def execute(steps: list[tuple[str, str | list[str]]], dry_run: bool) -> int:
    for kind, payload in steps:
        if kind == "write":
            assert isinstance(payload, str)
            path, _, text = payload.partition("\n")
            if dry_run:
                print(f"--- would write {path} ---\n{text}")
            else:
                Path(path).write_text(text)
                print(f"wrote {path}")
        else:
            assert isinstance(payload, list)
            if dry_run:
                print("would run:", " ".join(shlex.quote(c) for c in payload))
                continue
            print("running:", " ".join(shlex.quote(c) for c in payload))
            code = subprocess.call(payload)
            if code != 0 and payload[:2] not in (["sc", "stop"], ["launchctl", "bootout"]):
                print(f"failed with exit code {code}", file=sys.stderr)
                return code
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="splitter service")
    p.add_argument("action", choices=["install", "remove", "status", "start", "stop"])
    p.add_argument("--data-dir", default="")
    p.add_argument("--exe", default="", help="command that runs the server (default: this one)")
    p.add_argument("--platform", default=sys.platform, help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help="print what would be done")
    args = p.parse_args(argv)
    platform = "win32" if args.platform.startswith("win") else args.platform
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(platform)
    exe = shlex.split(args.exe) if args.exe else default_exe()
    target = Target(exe, data_dir, platform)
    if args.action == "install" and not args.dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)
    return execute(plan(target, args.action), args.dry_run)
