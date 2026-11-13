"""Benchmark package: leaderboard fetchers + shared measurement core.

Heavy fetchers (bs4/network) are imported lazily so that
``benchmarks.measurement`` and ``benchmarks.fallback`` stay importable
offline with only httpx installed.
"""

from .measurement import (  # noqa: F401
    BenchmarkRecord,
    StreamCollector,
    finalize_metrics,
    measure_openai_stream,
)


def __getattr__(name):
    # Lazy re-exports: fetchers pull heavy deps; fallback is also kept lazy so
    # `python -m benchmarks.fallback` doesn't double-import the module.
    if name in ("fetch_all", "fetch_source", "BENCHMARK_SOURCES"):
        from . import fetcher

        return getattr(fetcher, name)
    if name in ("build_fallback_config", "write_fallback_config"):
        from . import fallback

        return getattr(fallback, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
