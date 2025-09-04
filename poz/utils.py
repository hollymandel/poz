import asyncio
import functools
import inspect

from .task_factory import _parent_task_name


async def _cooperative_await(awaitable):
    # Cooperative tagging removed; we now only credit time from virtual_speedup.
    return await awaitable


def untracked(target):
    """Run an awaitable/callable such that Poz won't delay its resume.

    - One-liner: await poz.untracked(awaitable)
    - Decorator: @poz.untracked
    """
    # One-liner form: passed an awaitable
    if inspect.isawaitable(target):
        async def runner():
            tok = _parent_task_name.set(False)
            try:
                return await target
            finally:
                _parent_task_name.reset(tok)
        return runner()

    # Decorator form: passed a callable
    if callable(target):
        @functools.wraps(target)
        async def wrapper(*args, **kwargs):
            tok = _parent_task_name.set(False)
            try:
                res = target(*args, **kwargs)
                if inspect.isawaitable(res):
                    return await res
                return res
            finally:
                _parent_task_name.reset(tok)
        return wrapper

    raise TypeError("untracked expects a callable or awaitable")


# TaskGroup wrapper for 3.11+
if hasattr(asyncio, "TaskGroup"):
    class CooperativeTaskGroup:
        def __init__(self):
            self._tg = asyncio.TaskGroup()

        async def __aenter__(self):
            return await self._tg.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            return await _cooperative_await(self._tg.__aexit__(exc_type, exc, tb))

        def create_task(self, *args, **kwargs):
            return self._tg.create_task(*args, **kwargs)
