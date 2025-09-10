import asyncio
import functools
from contextlib import contextmanager
from asyncio import DefaultEventLoopPolicy

from .task_factory import _install_poz_task_factory 
from .delayable_handle import _install_handle_run_shim, _uninstall_handle_run_shim 
from .cooperatives import _poz_cooperative

if hasattr(asyncio, "TaskGroup"):
    from .cooperatives import CooperativeTaskGroup

# Keep originals so poz_context can patch/restore asyncio API
_orig_asyncio_api = {}
_orig_asyncio_methods = {}

def _install_asyncio_shims():
    global _orig_asyncio_api, _orig_asyncio_methods
    aio = asyncio

    # Module-level functions
    def _patch_fn(name, make_wrapper):
        if name not in _orig_asyncio_api:
            _orig_asyncio_api[name] = getattr(aio, name)
        orig = _orig_asyncio_api[name]
        setattr(aio, name, make_wrapper(orig))

    # Wrap selected asyncio functions so the cooperative ContextVar
    # is set BEFORE creating their awaitables.
    def _make_coop_wrapper(orig):
        async def _wrapped(*a, **k):
            tok = _poz_cooperative.set(True)
            try:
                return await orig(*a, **k)
            finally:
                _poz_cooperative.reset(tok)
        return _wrapped

    _patch_fn("gather", _make_coop_wrapper)
    _patch_fn("wait", _make_coop_wrapper)
    _patch_fn("wait_for", _make_coop_wrapper)
    _patch_fn("shield", _make_coop_wrapper)

    # TaskGroup (3.11+)
    if hasattr(aio, "TaskGroup"):
        # Replace class with cooperative variant
        if "TaskGroup" not in _orig_asyncio_api:
            _orig_asyncio_api["TaskGroup"] = aio.TaskGroup
        aio.TaskGroup = CooperativeTaskGroup

    # Class method helpers: wrap acquire/wait/get/put in cooperative tagging
    def _wrap_method(cls, method_name):
        key = (cls, method_name)
        if key in _orig_asyncio_methods:
            return
        orig = getattr(cls, method_name)
        _orig_asyncio_methods[key] = orig

        @functools.wraps(orig)
        async def wrapper(self, *args, **kwargs):
            tok = _poz_cooperative.set(True)
            try:
                return await orig(self, *args, **kwargs)
            finally:
                _poz_cooperative.reset(tok)

        setattr(cls, method_name, wrapper)

    _wrap_method(aio.Lock, "acquire")
    _wrap_method(aio.Semaphore, "acquire")
    if hasattr(aio, "BoundedSemaphore"):
        _wrap_method(aio.BoundedSemaphore, "acquire")
    _wrap_method(aio.Event, "wait")
    _wrap_method(aio.Condition, "wait")
    _wrap_method(aio.Condition, "wait_for")
    _wrap_method(aio.Queue, "get")
    _wrap_method(aio.Queue, "put")


def _uninstall_asyncio_shims():
    global _orig_asyncio_api, _orig_asyncio_methods
    aio = asyncio

    for name, orig in list(_orig_asyncio_api.items()):
        setattr(aio, name, orig)
    _orig_asyncio_api.clear()

    for (cls, method_name), orig in list(_orig_asyncio_methods.items()):
        setattr(cls, method_name, orig)
    _orig_asyncio_methods.clear()

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
        _install_asyncio_shims()
        asyncio.set_event_loop_policy(PozPolicy())
        yield
    finally:
        asyncio.set_event_loop_policy(old_policy)
        _uninstall_asyncio_shims()
        _uninstall_handle_run_shim()

# import asyncio
# from contextlib import contextmanager
# from asyncio import DefaultEventLoopPolicy

# from .task_factory import _install_poz_task_factory 
# from .delayable_handle import _install_handle_run_shim, _uninstall_handle_run_shim 

# class PozPolicy(DefaultEventLoopPolicy):
#     def new_event_loop(self):
#         loop = super().new_event_loop()
#         _install_poz_task_factory(loop)
#         return loop


# @contextmanager
# def poz_context():
#     """
#     Globally install Poz instrumentation for loops created inside this 'with' block.
#     Restores prior policy and Handle._run on exit.
#     """
#     old_policy = asyncio.get_event_loop_policy()
#     try:
#         _install_handle_run_shim()
#         asyncio.set_event_loop_policy(PozPolicy())
#         yield
#     finally:
#         asyncio.set_event_loop_policy(old_policy)
#         _uninstall_handle_run_shim()
