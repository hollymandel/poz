import asyncio
from .delayable_handle import _poz_ledger

def virtual_speedup(delta: float):
    # Update the ledger
    current_task = asyncio.current_task()
    other_tasks = [x for x in asyncio.all_tasks() if x is not current_task and not x.done()]

    for other in other_tasks:
        _poz_ledger[other.get_name()] += delta
