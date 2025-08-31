import asyncio
from contextlib import contextmanager
from asyncio import DefaultEventLoopPolicy

from task_factory import _install_poz_task_factory 
from delayable_handle import _install_handle_run_shim, _uninstall_handle_run_shim 


class PozPolicy(DefaultEventLoopPolicy):
    def new_event_loop(self):
        loop = super().new_event_loop()
        _install_poz_task_factory(loop)
        return loop


@contextmanager
def poz_context():
    """
    Globally install Poz instrumentation for loops created inside this 'with' block.
    Restores prior policy and Handle._run on exit.
    """
    old_policy = asyncio.get_event_loop_policy()
    try:
        _install_handle_run_shim()
        asyncio.set_event_loop_policy(PozPolicy())
        yield
    finally:
        asyncio.set_event_loop_policy(old_policy)
        _uninstall_handle_run_shim()
