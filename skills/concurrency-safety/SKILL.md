---
name: concurrency-safety
version: "1.2"
description: Rules for thread-safe Python components - locking shared state, atomic check-then-act, monotonic time, plus a concurrency test template that actually fails on a race.
applies_to: [code_generator, test_generator, code_reviewer]
triggers: [thread-safe, thread safe, threadsafe, concurrent, multithread, потокобезопас, многопоточ]
---
# Concurrency safety

- **CONC-01** Guard all mutable shared state with one `threading.Lock` per object; every read that
  depends on a mutation (including lazy refills/recomputations) happens under the same lock.
- **CONC-02** Check-then-act must be atomic: "is there capacity?" and "consume it" happen inside
  one critical section, otherwise two threads both pass the check.
- **CONC-03** Measure intervals with `time.monotonic()` - wall-clock `time.time()` jumps with NTP
  and DST. Still clamp negative elapsed time to zero if the clock is injectable.
- **CONC-04** Never block, sleep or call user callbacks while holding the lock.
- **CONC-05** Concurrency tests freeze time and hammer the object from several threads, then
  assert an exact invariant (e.g. total successful acquisitions == capacity).
- **CONC-06** A concurrency test must be able to FAIL. Under the GIL a naive thread test passes
  even without any lock, and the harness checks this by removing the locks from the implementation.
  Use the template below as is, adapting only the construction line and the call:
  - force thread switches with `sys.setswitchinterval(1e-6)` (restored afterwards);
  - a limit of at least 100 and far more calls than the limit (8 threads x 200 calls);
  - every thread makes a FIXED number of calls - never stop a worker early after a refusal;
  - a frozen clock (no refill during the test) and 10 repetitions of the whole scenario;
  - assert the exact invariant, not `<=`.

```python
import sys
import threading

import pytest


@pytest.fixture
def frequent_thread_switches():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


@pytest.mark.parametrize("attempt", range(10))
def test_concurrent_callers_never_exceed_the_limit(frequent_thread_switches, attempt):
    limited = TokenBucket(capacity=100, refill_rate=1, clock=lambda: 0.0)  # adapt: limit 100, frozen time
    start = threading.Barrier(8)
    granted = []

    def worker():
        start.wait()
        granted.append(sum(bool(limited.try_acquire(1)) for _ in range(200)))  # fixed call count

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(granted) == 100  # exact invariant: never more, never less
```
