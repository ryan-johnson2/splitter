"""Sidecar entry point for the desktop app (also handy for a plain "run it" binary).

The Tauri shell spawns this, passing the portable data directory, then waits
for the ``SPLITTER_READY <url>`` line on stdout before opening its window at
that URL. It binds all interfaces on port 8100 by default so a tablet on the
LAN can still open the same pages, falling back to an ephemeral port when
8100 is taken; the window itself always uses loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn


def _free_port(host: str, preferred: int) -> int:
    for candidate in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, candidate))
                return int(s.getsockname()[1])
        except OSError:
            continue
    raise SystemExit("no free port to bind")


def _log(msg: str) -> None:
    # stderr is a pipe to the shell; once the shell is gone a write raises. Never let
    # that stop the watchdog from exiting.
    with contextlib.suppress(OSError):
        print(msg, file=sys.stderr, flush=True)


def _parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, just not ours to signal
    if sys.platform.startswith("linux"):
        # A zombie still answers kill(pid, 0); read its state instead.
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except OSError:
            return False
    return True


def _watch_parent(pid: int) -> None:
    """Exit when the process that spawned us is gone, however it went.

    The shell kills us on a clean exit, but a crash or a signal to the shell
    would otherwise leave the server running headless. POSIX: poll the pid
    (zombie-aware on Linux); Windows: wait on the process handle. The pid is
    checked by liveness only — never against ``os.getppid()`` — because
    PyInstaller's one-file bootloader runs this interpreter as *its* child, so
    our real parent is the bootloader, not the shell.
    """
    _log(f"watchdog armed: exiting when pid {pid} goes away")
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            _log(f"watchdog: pid {pid} is gone, exiting")
            os._exit(0)
        return
    while True:
        time.sleep(1.0)
        if not _parent_alive(pid):
            _log(f"watchdog: pid {pid} is gone, exiting")
            os._exit(0)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="splitter-sidecar")
    p.add_argument("--data-dir", default=os.environ.get("SPLITTER_DATA_DIR", ""))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8100, help="0 = ephemeral")
    p.add_argument("--parent-pid", type=int, default=0, help="exit when this process dies")
    p.add_argument(
        "--service",
        action="store_true",
        help="run as a Windows service (started by the Service Control Manager)",
    )
    args = p.parse_args(argv)
    if args.service:
        return _run_as_windows_service(args)
    if args.parent_pid:
        threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()

    if args.data_dir:
        Path(args.data_dir).mkdir(parents=True, exist_ok=True)
        os.environ["SPLITTER_DATA_DIR"] = args.data_dir
    os.environ.setdefault("HOST", args.host)
    os.environ["SPLITTER_DESKTOP"] = "1"  # same PC as the game: pre-fill its LAN address
    port = _free_port(args.host, args.port) if args.port else _free_port(args.host, 0)
    os.environ["PORT"] = str(port)

    # Import after the environment is settled so Config() sees it.
    from splitter.app import create_app
    from splitter.config import Config

    cfg = Config()
    app = create_app(cfg)

    async def serve() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info"))

        # Announce once the socket is bound; the shell watches for this line, and
        # node.port / node.pid in the data dir let a later window find a server
        # that is already running (the service) instead of starting a second one.
        async def announce() -> None:
            while not server.started:
                await asyncio.sleep(0.05)
            print(f"SPLITTER_READY http://127.0.0.1:{cfg.port}/", flush=True)
            _write_marker(cfg.data_path, cfg.port)

        try:
            await asyncio.gather(server.serve(), announce())
        finally:
            _remove_marker(cfg.data_path)

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        sys.exit(0)


def _write_marker(data_dir: Path, port: int) -> None:
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "node.port").write_text(str(port))
        (data_dir / "node.pid").write_text(str(os.getpid()))


def _remove_marker(data_dir: Path) -> None:
    for name in ("node.port", "node.pid"):
        with contextlib.suppress(OSError):
            (data_dir / name).unlink()


def _run_as_windows_service(args: argparse.Namespace) -> None:
    """Service mode: the SCM handshake around the same server (needs pywin32).

    Untested outside Windows; the spike in docs/sync-plan.md phase 3. Runs the
    server in a thread and stops it when the SCM says so.
    """
    try:
        import servicemanager
        import win32event
        import win32service
        import win32serviceutil
    except ImportError:
        raise SystemExit("service mode needs pywin32 (pip install pywin32)") from None

    data_dir = args.data_dir

    class SplitterService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = "Splitter"
        _svc_display_name_ = "Splitter lap timer"

        def __init__(self, sargs: object) -> None:
            super().__init__(sargs)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server: uvicorn.Server | None = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.server is not None:
                self.server.should_exit = True
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self) -> None:
            servicemanager.LogInfoMsg("Splitter service starting")
            if data_dir:
                Path(data_dir).mkdir(parents=True, exist_ok=True)
                os.environ["SPLITTER_DATA_DIR"] = data_dir
            os.environ.setdefault("HOST", "0.0.0.0")
            os.environ["SPLITTER_DESKTOP"] = "1"
            port = _free_port("0.0.0.0", 8100)
            os.environ["PORT"] = str(port)
            from splitter.app import create_app
            from splitter.config import Config

            cfg = Config()
            self.server = uvicorn.Server(
                uvicorn.Config(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")
            )
            _write_marker(cfg.data_path, cfg.port)
            try:
                self.server.run()
            finally:
                _remove_marker(cfg.data_path)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(SplitterService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    main()
