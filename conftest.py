"""Shared pytest configuration.

Runs coroutine tests without requiring pytest-asyncio so that
`python -m pytest -q` works with a bare pytest install. When pytest-asyncio
is installed its own hook takes over and this fallback stays inactive.
"""

import asyncio
import inspect

import pytest

try:  # pragma: no cover - depends on the environment
    import pytest_asyncio  # noqa: F401

    HAS_PYTEST_ASYNCIO = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_PYTEST_ASYNCIO = False


if not HAS_PYTEST_ASYNCIO:

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem):
        func = pyfuncitem.obj
        if not inspect.iscoroutinefunction(func):
            return None
        argnames = pyfuncitem._fixtureinfo.argnames
        kwargs = {name: pyfuncitem.funcargs[name] for name in argnames if name in pyfuncitem.funcargs}
        asyncio.run(func(**kwargs))
        return True
