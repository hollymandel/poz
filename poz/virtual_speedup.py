import asyncio
from .delayable_handle import _poz_ledger, _poz_suspended_start
from .task_factory import _parent_task_name


def virtual_speedup(delta: float):
    # Update the ledger and record the start time of this speedup's credit window
    loop = asyncio.get_running_loop()
    now = loop.time()

    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        tid = other.get_name()
        _poz_ledger[tid] += delta
        _poz_suspended_start[tid] = now
