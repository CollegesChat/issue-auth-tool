import threading

import issue_auth_tool.utils as utils


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def test_rate_limit_enforces_multiple_windows(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(utils.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(utils.time, 'sleep', clock.sleep)

    call_times: list[float] = []

    @utils.rate_limit(2, 1.0)
    def limited(value: int) -> int:
        call_times.append(clock.now)
        return value

    assert [limited(i) for i in range(5)] == [0, 1, 2, 3, 4]
    assert call_times == [100.0, 100.0, 101.0, 101.0, 102.0]
    assert clock.sleeps == [1.0, 1.0]


def test_rate_limit_waits_outside_lock_under_concurrency(monkeypatch):
    release_sleep = threading.Event()
    both_sleeping = threading.Event()
    sleep_lock = threading.Lock()
    sleep_calls: list[float] = []

    monkeypatch.setattr(utils.time, 'monotonic', lambda: 100.0)

    def blocking_sleep(seconds: float) -> None:
        with sleep_lock:
            sleep_calls.append(seconds)
            if len(sleep_calls) == 2:
                both_sleeping.set()
        assert release_sleep.wait(1)

    monkeypatch.setattr(utils.time, 'sleep', blocking_sleep)

    start_barrier = threading.Barrier(3)
    results: list[int] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    @utils.rate_limit(1, 1.0)
    def limited(value: int) -> int:
        with result_lock:
            results.append(value)
        return value

    def worker(value: int) -> None:
        try:
            start_barrier.wait()
            limited(value)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()

    assert both_sleeping.wait(1)
    assert sorted(sleep_calls) == [1.0, 2.0]

    release_sleep.set()
    for thread in threads:
        thread.join(1)

    assert not errors
    assert sorted(results) == [0, 1, 2]
