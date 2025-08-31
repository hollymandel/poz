import asyncio
from collections import defaultdict

_poz_ledger = defaultdict(float)
_orig_run = None

def _install_handle_run_shim():
    global _orig_run
    _orig_run = asyncio.Handle._run

    loop = asyncio.get_event_loop()

    def _run_shim(self):
        debt = _poz_ledger[self._context._parent_task_name]

        try:
            if debt <= 0:
                orig_run(self)
                return
            
            else:
                loop.call_later(debt, self._callback, *self._args, context=self._context)
        finally:
            _poz_ledger[self._context._parent_task_name] = 0

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




