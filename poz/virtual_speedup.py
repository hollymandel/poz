import asyncio
import time
from .delayable_handle import _poz_ledger
from .task_factory import _parent_task_name

_speedup_time = [-1]

def virtual_speedup(delta: float):
    # Update the ledger
    if (time.perfcounter-_speedup_time[0]) < delta: # units?
        raise AssertionError("new speedup hit before old completed")
    else:
        _speedup_time[0] = time.perfcounter()

    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        _poz_ledger[other.get_name()] += delta
        


