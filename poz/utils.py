import asyncio
import functools
import inspect
import contextvars
# Avoid importing delayable_handle here to prevent circular imports.

# Track cooperative-await nesting per Poz task id. Value is an int depth.
_poz_cooperative_depth = {}
# Context flag for cooperative sections; captured by handles scheduled within.
_poz_cooperative = contextvars.ContextVar("_poz_cooperative", default=False)


async def _cooperative_await(awaitable):
    # Mark this task as inside a cooperative await while awaiting the target
    tok = _poz_cooperative.set(True)
    cur = asyncio.current_task()
    tid = cur.get_name() if cur else None
    if tid:
        _poz_cooperative_depth[tid] = _poz_cooperative_depth.get(tid, 0) + 1
    try:
        return await awaitable
    finally:
        if tid:
            depth = _poz_cooperative_depth.get(tid, 0)
            if depth <= 1:
                _poz_cooperative_depth.pop(tid, None)
            else:
                _poz_cooperative_depth[tid] = depth - 1
        _poz_cooperative.reset(tok)


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
