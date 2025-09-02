import asyncio
import functools
import inspect
from contextvars import ContextVar

from .task_factory import _parent_task_name

# Tag the suspension leading into an await as "cooperative" so the handle shim
# can credit real time and avoid penalizing suspended tasks.
_poz_cooperative = ContextVar("_poz_cooperative", default=False)
_poz_suspend_start = ContextVar("_poz_suspend_start", default=None)


async def _cooperative_await(awaitable):
    loop = asyncio.get_running_loop()
    tok1 = _poz_cooperative.set(True)
    tok2 = _poz_suspend_start.set(loop.time())
    try:
        return await awaitable
    finally:
        _poz_suspend_start.reset(tok2)
        _poz_cooperative.reset(tok1)


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


# Cooperative wrappers for common asyncio primitives
# async def sleep(delay, *args, **kwargs):
#     return await _cooperative_await(asyncio.sleep(delay, *args, **kwargs))


async def gather(*aws, **kwargs):
    return await _cooperative_await(asyncio.gather(*aws, **kwargs))


async def wait(fs, *args, **kwargs):
    return await _cooperative_await(asyncio.wait(fs, *args, **kwargs))


async def wait_for(aw, timeout):
    return await _cooperative_await(asyncio.wait_for(aw, timeout))


async def shield(aw):
    return await _cooperative_await(asyncio.shield(aw))


# Synchronization helpers
async def lock_acquire(lock: asyncio.Lock):
    return await _cooperative_await(lock.acquire())

async def lock_release(lock: asyncio.Lock):
    return await _cooperative_await(lock.release())

async def sem_acquire(sem: asyncio.Semaphore):
    return await _cooperative_await(sem.acquire())


async def event_wait(ev: asyncio.Event):
    return await _cooperative_await(ev.wait())


async def condition_wait(cond: asyncio.Condition):
    return await _cooperative_await(cond.wait())


async def condition_wait_for(cond: asyncio.Condition, predicate, timeout=None):
    if timeout is None:
        return await _cooperative_await(cond.wait_for(predicate))
    return await _cooperative_await(cond.wait_for(predicate, timeout))


async def queue_get(q: asyncio.Queue):
    return await _cooperative_await(q.get())


async def queue_put(q: asyncio.Queue, item):
    return await _cooperative_await(q.put(item))


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
