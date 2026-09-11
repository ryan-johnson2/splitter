from __future__ import annotations

from splitter.core.telemetry import TelemetryBuffer


def fill(buf: TelemetryBuffer, n: int, dt_ms: int = 50, speed: float = 10.0) -> None:
    for i in range(n):
        t = i * dt_ms
        x = speed * t / 1000
        buf.add(t, (x, 0.0, 0.0), (speed, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def test_segment_stats_and_distance() -> None:
    buf = TelemetryBuffer()
    fill(buf, 41)  # 0..2000 ms at 10 m/s → 20 m
    stats = buf.segment(-1, 2000)
    assert stats is not None
    assert stats.samples == 41
    assert stats.max_speed == 10.0 and stats.avg_speed == 10.0
    assert abs(stats.distance_m - 20.0) < 1e-6
    half = buf.segment(1000, 2000)
    assert half is not None and half.samples == 20
    assert buf.segment(5000, 6000) is None


def test_downsampling() -> None:
    buf = TelemetryBuffer()
    fill(buf, 120, dt_ms=1000 // 60)  # ~2 s at 60 Hz
    assert 38 <= len(buf.downsampled(20)) <= 41  # 120 x 16 ms ≈ 1.9 s at 20 Hz
    assert len(buf.downsampled(0)) == 120


def test_clock_alignment() -> None:
    buf = TelemetryBuffer()
    # First IMU frame arrives 120 ms into the race with a game clock of 500000.
    assert buf.race_ms_for(500000.0, 120) == 120
    assert buf.race_ms_for(500016.7, 999) == 137  # game-clock spacing wins over the fallback
    buf.clear()
    assert buf.race_ms_for(1000.0, 0) == 0
