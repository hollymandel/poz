import asyncio
from .delayable_handle import _poz_ledger, _poz_suspended_start, _poz_suspended_is_coop
from .utils import _poz_cooperative_depth


def virtual_speedup(delta: float):
    # Update the ledger and record the start time of this speedup's credit window
    loop = asyncio.get_running_loop()
    now = loop.time()

    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        tid = other.get_name()
        _poz_ledger[tid] += delta
        # Record window start for all tasks to bound penalties in time,
        # but only mark cooperative tasks to receive elapsed-time credit.
        _poz_suspended_start[tid] = now
        _poz_suspended_is_coop[tid] = (_poz_cooperative_depth.get(tid, 0) > 0)
