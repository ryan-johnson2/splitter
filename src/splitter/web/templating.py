"""Shared Jinja2 templates instance plus the globals every page uses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from splitter.core.timeparse import DEFAULT_TIME_FORMAT, format_delta, format_ms, format_speed
from splitter.version import __version__


def _site_context(request: Any) -> dict[str, Any]:
    settings = getattr(request.app.state, "settings", None)
    bridge = getattr(request.app.state, "bridge", None)
    style = (settings.get("time_format") if settings else "") or DEFAULT_TIME_FORMAT
    return {
        "brand_name": (settings.get("brand_name").strip() if settings else "") or "Splitter",
        "app_version": __version__,
        "game_connected": bool(bridge and bridge.connected),
        "time_format": style,
        "pace_yellow_s": settings.get_float("pace_yellow_s") if settings else 2.0,
        # Per-request override of the global so every page honours the setting.
        "format_ms": lambda ms: format_ms(ms, style),
    }


templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"), context_processors=[_site_context]
)


def utc_iso(dt: datetime | None) -> str:
    """Naive-UTC datetime → ISO string with a Z, for ``<time datetime>``."""
    if dt is None:
        return ""
    return dt.replace(microsecond=0).isoformat() + "Z"


def delta_class(ms: int | None) -> str:
    if ms is None:
        return ""
    return "ahead" if ms < 0 else "behind" if ms > 0 else ""


def mode_label(mode: str) -> str:
    """``THREE_LAP_SINGLE_CLASS`` → ``Three lap single class``."""
    return mode.replace("_", " ").capitalize() if mode else ""


templates.env.globals.update(
    format_ms=format_ms,
    format_delta=format_delta,
    format_speed=format_speed,
    delta_class=delta_class,
    mode_label=mode_label,
    utc_iso=utc_iso,
)


def redirect_with_flash(
    url: str, notice: str = "", error: str = "", status_code: int = 303
) -> RedirectResponse:
    params = {k: v for k, v in (("notice", notice), ("error", error)) if v}
    if params:
        base, hash_sep, fragment = url.partition("#")
        base = f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"
        url = f"{base}{hash_sep}{fragment}"
    return RedirectResponse(url, status_code=status_code)
