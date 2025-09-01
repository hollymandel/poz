import asyncio
import functools
from .delayable_handle import _poz_ledger
from .task_factory import _parent_task_name


def virtual_speedup(delta: float):
    # Update the ledger
    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        _poz_ledger[other.get_name()] += delta


def untracked(coro_fn):
    if not asyncio.iscoroutinefunction(coro_fn):
        raise TypeError("@untracked must decorate an async function")

    @functools.wraps(coro_fn)
    async def wrapper(*args, **kwargs):
        # Mark the current task as untracked for virtual_speedup targeting
        task = asyncio.current_task()
        if task is not None:
            setattr(task, "_poz_tracked", False)

        # Clear Poz task id from context so callbacks scheduled by this task
        # won't be delayed by the handle shim
        tok = _parent_task_name.set(False)
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            _parent_task_name.reset(tok)
