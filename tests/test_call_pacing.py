"""The call pacer only ever delays, and delays by the amount it promises."""

from __future__ import annotations

import asyncio

import pytest

from linkedin_scraper_mcp.call_pacing import CallPacer


class FakeTime:
    """A clock the test moves by hand; sleeping advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _pacer(fake: FakeTime, **kwargs) -> CallPacer:
    kwargs.setdefault("jitter_seconds", 0.0)
    return CallPacer(clock=fake.clock, sleep=fake.sleep, rng=lambda: 0.5, **kwargs)


def test_the_first_call_never_waits():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=3.0, max_calls_per_minute=12)

    assert asyncio.run(pacer.wait()) == 0.0
    assert fake.slept == []


def test_a_call_right_after_another_waits_out_the_gap():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=3.0, max_calls_per_minute=0)
    asyncio.run(pacer.wait())
    pacer.finished()

    waited = asyncio.run(pacer.wait())

    assert waited == pytest.approx(3.0)
    assert fake.slept == [pytest.approx(3.0)]


def test_the_gap_counts_from_the_end_of_the_previous_call_not_its_start():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=3.0, max_calls_per_minute=0)
    asyncio.run(pacer.wait())
    fake.now += 10.0  # the call itself took ten seconds
    pacer.finished()
    fake.now += 1.0

    assert asyncio.run(pacer.wait()) == pytest.approx(2.0)


def test_a_call_after_a_long_pause_does_not_wait():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=3.0, max_calls_per_minute=0)
    asyncio.run(pacer.wait())
    pacer.finished()
    fake.now += 30.0

    assert asyncio.run(pacer.wait()) == 0.0


def test_the_jitter_adds_to_the_gap():
    fake = FakeTime()
    pacer = CallPacer(
        min_gap_seconds=3.0,
        jitter_seconds=2.0,
        max_calls_per_minute=0,
        clock=fake.clock,
        sleep=fake.sleep,
        rng=lambda: 0.5,  # half of the jitter
    )
    asyncio.run(pacer.wait())
    pacer.finished()

    assert asyncio.run(pacer.wait()) == pytest.approx(4.0)


def test_the_per_minute_ceiling_holds_the_call_until_the_window_frees():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=0.0, max_calls_per_minute=3)
    start = fake.now
    for _ in range(3):
        asyncio.run(pacer.wait())
        pacer.finished()
        fake.now += 1.0

    waited = asyncio.run(pacer.wait())

    # The first call began at `start`; the fourth may begin once it is 60s old.
    assert fake.now == pytest.approx(start + 60.0)
    assert waited == pytest.approx(57.0)


def test_the_ceiling_is_a_rolling_window_not_a_reset():
    fake = FakeTime()
    pacer = _pacer(fake, min_gap_seconds=0.0, max_calls_per_minute=2)
    asyncio.run(pacer.wait())
    fake.now += 40.0
    asyncio.run(pacer.wait())
    fake.now += 25.0  # the first call is now 65s old, the second 25s

    assert asyncio.run(pacer.wait()) == 0.0


def test_a_pacer_with_everything_off_never_delays():
    fake = FakeTime()
    pacer = _pacer(
        fake, min_gap_seconds=0.0, jitter_seconds=0.0, max_calls_per_minute=0
    )

    assert pacer.enabled is False
    for _ in range(50):
        assert asyncio.run(pacer.wait()) == 0.0
        pacer.finished()
    assert fake.slept == []


class TestReadingTheEnvironment:
    def test_defaults_when_nothing_is_set(self):
        pacer = CallPacer.from_env({})

        assert pacer.enabled is True
        assert pacer._min_gap == 3.0
        assert pacer._jitter == 1.5
        assert pacer._max_per_minute == 12

    def test_values_are_taken_from_the_variables(self):
        pacer = CallPacer.from_env(
            {
                "LINKEDIN_MIN_CALL_GAP_SECONDS": "5",
                "LINKEDIN_CALL_GAP_JITTER_SECONDS": "0",
                "LINKEDIN_MAX_CALLS_PER_MINUTE": "6",
            }
        )

        assert (pacer._min_gap, pacer._jitter, pacer._max_per_minute) == (5.0, 0.0, 6)

    def test_zero_turns_everything_off(self):
        pacer = CallPacer.from_env(
            {
                "LINKEDIN_MIN_CALL_GAP_SECONDS": "0",
                "LINKEDIN_CALL_GAP_JITTER_SECONDS": "0",
                "LINKEDIN_MAX_CALLS_PER_MINUTE": "0",
            }
        )

        assert pacer.enabled is False

    @pytest.mark.parametrize("bad", ["abc", "-4", "nan", ""])
    def test_an_unusable_value_falls_back_to_the_default(self, bad):
        pacer = CallPacer.from_env({"LINKEDIN_MIN_CALL_GAP_SECONDS": bad})

        assert pacer._min_gap == 3.0


def test_the_caller_is_told_the_delay_before_the_sleep():
    fake = FakeTime()
    told: list[tuple[float, int]] = []

    async def on_delay(delay: float) -> None:
        told.append((delay, len(fake.slept)))  # how many sleeps had happened

    pacer = _pacer(fake, min_gap_seconds=3.0, max_calls_per_minute=0)
    asyncio.run(pacer.wait(on_delay))
    pacer.finished()
    asyncio.run(pacer.wait(on_delay))

    assert told == [(pytest.approx(3.0), 0)]  # once, and before it slept


def test_no_notice_when_there_is_nothing_to_wait_for():
    fake = FakeTime()
    told: list[float] = []

    async def on_delay(delay: float) -> None:
        told.append(delay)

    asyncio.run(_pacer(fake, min_gap_seconds=3.0).wait(on_delay))

    assert told == []
