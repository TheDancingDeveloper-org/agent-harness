"""Portable service-process sampling has no session or filesystem dependency."""

from agent_harness.process_metrics import ProcessMetricsSampler


def test_sampler_uses_monotonic_uptime_and_injected_process_observers() -> None:
    wall = iter([100.0, 121.0])
    monotonic = iter([50.0, 70.5])
    sampler = ProcessMetricsSampler(
        wall_time=lambda: next(wall),
        monotonic=lambda: next(monotonic),
        cpu_time=lambda: 3.25,
        pid=lambda: 42,
        thread_count=lambda: 7,
    )

    sample = sampler.sample()

    assert sample.started_at == 100.0
    assert sample.sampled_at == 121.0
    assert sample.uptime_seconds == 20.5
    assert sample.cpu_seconds == 3.25
    assert sample.pid == 42
    assert sample.thread_count == 7
