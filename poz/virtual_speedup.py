import asyncio
import time
from .delayable_handle import _poz_ledger, _speedup_time
from .task_factory import _parent_task_name

def virtual_speedup(delta: float):
    # Update the ledger
    if (time.perf_counter()-_speedup_time[0]) < delta: # units?
        # TODO: ALSO ASSERT NO REMAINING DELTAS IN LEDGER?
        raise AssertionError("new speedup encountered before old completed. This is currently not allowed.")
    else:
        _speedup_time[0] = time.perf_counter()

    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        _poz_ledger[other.get_name()] += delta
        


