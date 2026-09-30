"""Bounded per-worker latency diagnostics. Never retain payloads or account IDs."""

import threading
import time
from collections import defaultdict, deque
from functools import wraps

_samples = defaultdict(lambda: deque(maxlen=200))
_lock = threading.Lock()


def record(name, seconds):
    with _lock:
        _samples[name].append(round(seconds * 1000, 2))


def timed(name):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            start = time.monotonic()
            try:
                return function(*args, **kwargs)
            finally:
                record(name, time.monotonic() - start)

        return wrapped

    return decorator


def summary():
    with _lock:
        return {
            name: {
                "samples": len(values),
                "p50_ms": sorted(values)[len(values) // 2],
                "p95_ms": sorted(values)[min(len(values) - 1, int(len(values) * 0.95))],
            }
            for name, values in _samples.items()
            if values
        }
