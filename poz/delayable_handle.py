import asyncio
import time
from collections import defaultdict
from .task_factory import _parent_task_name
from .cooperatives import _poz_cooperative

_poz_ledger = defaultdict(float)
_speedup_time = [-1]
_orig_run = None

def _install_handle_run_shim():
    global _orig_run
    _orig_run = asyncio.Handle._run

    def _run_shim(self):
        
        task_id = self._context.get(_parent_task_name)
        is_cooperative = self._context.get(_poz_cooperative)
        is_internal = getattr(self._callback, "_poz_internal", False)

        # If we can't attribute this handle to a poz-tracked task, or it's an
        # internal poz callback (like cleanup), just run it without affecting debt.
        if not task_id or is_internal:
            return _orig_run(self)

        debt = _poz_ledger[task_id]

        try:
            if debt <= 0:
                _orig_run(self)
            elif is_cooperative:
                print(f"{task_id} found to be cooperative")
                now = time.perf_counter()
                if (now - _speedup_time[0]) < debt:
                    remaining_debt = debt - (now - _speedup_time[0])
                    print(f"Rescheduling {self._callback} for {remaining_debt} seconds!")
                    self._loop.call_later(debt, self._callback, *self._args, context=self._context)   
                else:
                    print(f"Debt exceeded, not rescheduling {self._callback}")
                    _orig_run(self)
            else:
                # Reschedule on the same loop this handle belongs to
                print(f"Rescheduling {self._callback} for {debt} seconds")
                self._loop.call_later(debt, self._callback, *self._args, context=self._context)
        finally:
            # Decrement any applied debt for this task. If we didn't reschedule,
            # debt will be zero and this is a no-op.
            _poz_ledger[task_id] -= debt

    asyncio.Handle._run = _run_shim

def _uninstall_handle_run_shim():
    global _orig_run
    if _orig_run is not None:
        asyncio.Handle._run = _orig_run
    else:
        # For now, we are choosing to make install/uninstall raise errors
        # rather than being safe, because if there is some gap in our understanding
        # of the program flow we want to catch it
        raise AssertionError("Trying to reset Handle._run but stored value is None, this shouldn't occur")
    
    _orig_run = None


