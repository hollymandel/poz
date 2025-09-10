# poz/utils.py
import asyncio
import functools
import inspect
from .task_factory import _parent_task_name

def _enter_untracked():
    task = asyncio.current_task()
    prev_tracked = getattr(task, "_poz_tracked", True) if task else True
    if task:
        setattr(task, "_poz_tracked", False)
    tok = _parent_task_name.set(False)
    return task, prev_tracked, tok

def _exit_untracked(task, prev_tracked, tok):
    _parent_task_name.reset(tok)
    if task:
        setattr(task, "_poz_tracked", prev_tracked)

def untracked(target):
    # One-liner: passed an awaitable, return a coroutine to await
    if inspect.isawaitable(target):
        async def runner():
            task, prev, tok = _enter_untracked()
            try:
                return await target
            finally:
                _exit_untracked(task, prev, tok)
        return runner()

    # Decorator: passed a callable, wrap it
    if callable(target):
        @functools.wraps(target)
        async def wrapper(*args, **kwargs):
            task, prev, tok = _enter_untracked()
            try:
                res = target(*args, **kwargs)
                if inspect.isawaitable(res):
                    return await res
                return res
            finally:
                _exit_untracked(task, prev, tok)
        return wrapper

    raise TypeError("untracked expects a callable or awaitable")



def untracked(target):
    """Run an awaitable/callable such that Poz won't delay its resume.

    - One-liner: await poz.untracked(awaitable)
    - Decorator: @poz.untracked
    """
    # One-liner form: passed an awaitable
    if inspect.isawaitable(target):
        async def runner():
            tok = _parent_task_name.set(False)
            try:
                return await target
            finally:
                _parent_task_name.reset(tok)
        return runner()

    # Decorator form: passed a callable
    if callable(target):
        @functools.wraps(target)
        async def wrapper(*args, **kwargs):
            tok = _parent_task_name.set(False)
            try:
                res = target(*args, **kwargs)
                if inspect.isawaitable(res):
                    return await res
                return res
            finally:
                _parent_task_name.reset(tok)
        return wrapper

    raise TypeError("untracked expects a callable or awaitable")
