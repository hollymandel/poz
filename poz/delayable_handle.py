import asyncio
import time
from collections import defaultdict
from .task_factory import _parent_task_name

_poz_ledger = defaultdict(float)
_orig_run = None

__all__ = ["_install_handle_run_shim", "_uninstall_handle_run_shim"]

def _install_handle_run_shim():
    global _orig_run
    _orig_run = asyncio.Handle._run

    def _run_shim(self):
        # Identify the owning task for this handle, preferring a bound Task callback,
        # and falling back to the captured context.
        task_name = None
        try:
            owner = getattr(self._callback, "__self__", None)
            if isinstance(owner, asyncio.Task):
                task_name = owner.get_name()
            else:
                task_name = self._context.get(_parent_task_name)
        except Exception:
            task_name = self._context.get(_parent_task_name)

        try:
            debt = _poz_ledger[task_name]
        except Exception as e:
            print(f"[ {time.time()} ] error getting debt for task {task_name}: {e}")
            raise e
        if debt is None:
            print(f"[ {time.time()} ] No debt found for task {task_name}")
            raise ValueError(f"No debt found for task {task_name}")

        try:
            if debt <= 0:
                print(f"debt <= 0 for task {current_task_name}")
                _orig_run(self)
                return
            else:
                # Debugging aid: uncomment to trace delays
                # print(f"[ {time.time():0.6f} ] delaying handle for task={task_name} by {debt}s")
                self._loop.call_later(debt, self._context.run, self._callback, *self._args)
        finally:
            try:
                _poz_ledger[task_name] = 0
            except Exception as e:
                print(f"[ {time.time()} ] error resetting ledger: {e}")

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

