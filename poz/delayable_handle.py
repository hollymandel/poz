import asyncio
from collections import defaultdict
from .task_factory import _parent_task_name

_poz_ledger = defaultdict(float)
# Track the moment a virtual speedup was applied to each task's debt.
# Keyed by Poz task id (name), value is loop.monotonic time.
_poz_suspended_start = {}
# Track whether the task was in a cooperative await when the window started.
_poz_suspended_is_coop = {}
_orig_run = None

def _install_handle_run_shim():
    global _orig_run
    _orig_run = asyncio.Handle._run

    def _run_shim(self):
        task_id = self._context.get(_parent_task_name)
        is_internal = getattr(self._callback, "_poz_internal", False)

        # If we can't attribute this handle to a poz-tracked task, or it's an
        # internal poz callback (like cleanup), just run it without affecting debt.
        if not task_id or is_internal:
            return _orig_run(self)

        debt = _poz_ledger[task_id]

        try:
            if debt > 0:
                debt_start = _poz_suspended_start.get(task_id)
                was_coop = _poz_suspended_is_coop.get(task_id, False)
                if debt_start is None:
                    # No window recorded; fall back to absolute delay
                    self._loop.call_later(debt, self._callback, *self._args, context=self._context)
                elif was_coop:
                    # Cooperative: give credit since window start
                    elapsed = max(0.0, self._loop.time() - debt_start)
                    remaining = max(0.0, debt - elapsed)
                    if remaining > 0:
                        self._loop.call_later(remaining, self._callback, *self._args, context=self._context)
                    else:
                        _orig_run(self)
                else:
                    # Non-cooperative: absolute penalty only during the window
                    if self._loop.time() < (debt_start + debt):
                        self._loop.call_later(debt, self._callback, *self._args, context=self._context)
                    else:
                        _orig_run(self)
            else:
                _orig_run(self)
        finally:
            # Clear any applied debt for this task (paid by elapsed time or reschedule)
            _poz_ledger[task_id] = 0.0
            # Also clear any recorded window markers; new speedups will set again.
            _poz_suspended_start.pop(task_id, None)
            _poz_suspended_is_coop.pop(task_id, None)

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
