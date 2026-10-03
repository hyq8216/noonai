"""Advance workflow polling deterministically without wall-clock sleeps."""
from datetime import datetime, timedelta, timezone


class ManualClock:
    def __init__(self):
        self.value = datetime.now(timezone.utc)
        self.elapsed = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds=6):
        self.value += timedelta(seconds=seconds)
        self.elapsed += seconds


def install_clock(auto):
    clock = ManualClock()
    auto.clock = clock
    auto.monotonic = lambda: clock.elapsed
    original = auto.tick

    def tick():
        clock.advance()
        return original()

    auto.tick = tick
    return clock
