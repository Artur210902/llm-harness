---
name: concurrency-safety
version: "1.0"
description: Rules for thread-safe Python components - locking shared state, atomic check-then-act, monotonic time, deterministic concurrency tests.
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
