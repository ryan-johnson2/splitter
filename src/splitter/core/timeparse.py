"""Millisecond formatting shared by templates, the live feed and the CLI."""

from __future__ import annotations

TIME_FORMATS: dict[str, str] = {
    "seconds": "seconds only, like the game (144.359)",
    "mmss": "minutes and seconds (2:24.359)",
    "both": "both (2:24.359 (144.359))",
}
DEFAULT_TIME_FORMAT = "seconds"


def format_ms(ms: int | None, style: str = DEFAULT_TIME_FORMAT) -> str:
    """Render milliseconds in the chosen style (see ``TIME_FORMATS``).

    VelociDrone shows every time in plain seconds, so that is the default;
    ``mmss`` gives ``M:SS.mmm`` (``SS.mmm`` under a minute), ``both`` shows
    the seconds in parentheses after the minutes form.
    """
    if ms is None or ms < 0:
        return "--"
    seconds_only = f"{ms / 1000:.3f}"
    if style == "seconds":
        return seconds_only
    minutes, rem = divmod(ms, 60_000)
    mmss = f"{minutes}:{rem / 1000:06.3f}" if minutes else seconds_only
    if style == "both" and minutes:
        return f"{mmss} ({seconds_only})"
    return mmss


def format_delta(ms: int | None) -> str:
    """Render a split as ``-0.123`` / ``+0.123`` (``--`` when unknown)."""
    if ms is None:
        return "--"
    sign = "-" if ms < 0 else "+"
    return f"{sign}{abs(ms) / 1000:.3f}"


def format_speed(mps: float | None) -> str:
    """Metres/second → ``km/h`` with no decimals."""
    if mps is None:
        return "--"
    return f"{mps * 3.6:.0f}"
