from asyncio import BaseEventLoop, tasks, DefaultEventLoopPolicy
import asyncio
from typing import Optional
import contextvars
import weakref
import itertools
import logging
from .cooperatives import _poz_cooperative
from .cooperatives import _poz_cooperative

# Guard against multiple imports: preserve existing registries
try:  # type: ignore[name-defined]
    _poz_task_namer  # noqa: F401
except NameError:  # fresh import or reload without prior state
    _poz_task_namer = itertools.count()

try:  # type: ignore[name-defined]
    _seen_names  # noqa: F401
except NameError:
    _seen_names = set()

def _create_and_register_name(): 
    name = f"poz-{next(_poz_task_namer)}"

    # TODO: could become expensive
    # also nominally permits infintie recursion though shouldn't
    if name in _seen_names:
        return _create_and_register_name()

    _seen_names.add(name)
    return name

# TODO: these should live inside the event loop I think?
# _task_ledger = weakref.WeakKeyDictionary()
_parent_task_name = contextvars.ContextVar("_parent_task_name", default=False)

def _install_poz_task_factory(loop: Optional[asyncio.AbstractEventLoop] = None):
    loop = loop or asyncio.get_running_loop()
    prev_task_factory = loop.get_task_factory()

    def _poz_task_factory(loop, coro, context=None, name=None):
        if name is not None:
            if name in _seen_names:
                raise ValueError(f"Name supplied to task factory already exists in task name register: {name}")
            task_id = name
            _seen_names.add(task_id)
        else:
            task_id = _create_and_register_name()
        print(f"scheduling {task_id}")
        # inject task_id into context so that it will be captured by child callbacks. Can't
        # use the task itself as an identifier because context is captured during task instantiation.
        try:
            if context is not None:
                ctx = context.copy()

                def _init_ctx():
                    _parent_task_name.set(task_id)
                    # Ensure new tasks never inherit a cooperative tag
                    _poz_cooperative.set(False)

                ctx.run(_init_ctx)
                task = (prev_task_factory(loop, coro, context=ctx, name=task_id)
                        if prev_task_factory else asyncio.tasks.Task(coro, loop=loop, context=ctx, name=task_id))
            else:
                tok_name = _parent_task_name.set(task_id)
                tok_coop = _poz_cooperative.set(False)
                try:
                    task = (prev_task_factory(loop, coro, name=task_id)
                            if prev_task_factory else asyncio.tasks.Task(coro, loop=loop, name=task_id))
                finally:
                    _poz_cooperative.reset(tok_coop)
                    _parent_task_name.reset(tok_name)

            # clean _seen_names to speed lookups
            # mark the cleanup callback so the poz handle shim can ignore it
            def _poz_cleanup_cb(t):
                _seen_names.discard(t.get_name())

            # Mark as internal so delay shim won't treat it as user work
            _poz_cleanup_cb._poz_internal = True  # type: ignore[attr-defined]

            task.add_done_callback(_poz_cleanup_cb)
            return task
        except Exception:
            _seen_names.discard(task_id)
            raise

    loop.set_task_factory(_poz_task_factory)
