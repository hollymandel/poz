import asyncio
import collections
import functools
import heapq
import weakref
import contextvars
from typing import Optional
import time
import sys
from contextlib import contextmanager
import inspect

# ─────────────────────────────────────────────────────────────
# Capture ORIGINAL asyncio primitives BEFORE monkey-patching
# ─────────────────────────────────────────────────────────────
_ORIG_Lock = asyncio.Lock
_ORIG_Semaphore = asyncio.Semaphore
_ORIG_BoundedSemaphore = asyncio.BoundedSemaphore
_ORIG_Queue = asyncio.Queue

# ─────────────────────────────────────────────────────────────
# Callsite fingerprint (unchanged)
# ─────────────────────────────────────────────────────────────

def _poz_callsite_key():
    """
    Return a stable key for the *user* call site of virtual_speedup():
    (filename, lineno, funcname, bytecode_offset)
    """
    modfile = sys.modules[PozLoop.__module__].__file__  # our module file
    f = sys._getframe(1)  # caller of PozLoop.virtual_speedup (classmethod)
    while f and f.f_code.co_filename == modfile:
        f = f.f_back
    if f is None:  # fallback
        f = sys._getframe(1)
    c = f.f_code
    return (c.co_filename, f.f_lineno, c.co_name, f.f_lasti)

# Avoid proactor loop on windows machine
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_TARGET_REF: Optional[weakref.ReferenceType[asyncio.Task]]  = None
def _get_target():
    return _TARGET_REF() if _TARGET_REF else None
def _set_target(t: Optional[asyncio.Task]):
    global _TARGET_REF
    _TARGET_REF = weakref.ref(t) if t is not None else None

_poz_flag = contextvars.ContextVar("poz_flag", default=False)
_gate_bypass = contextvars.ContextVar("poz_gate_bypass", default=False)

def _unwrap(cb):
    while isinstance(cb, functools.partial):
        cb = cb.func
    return cb

def _task_from_callback(cb):
    cb = _unwrap(cb)
    self_obj = getattr(cb, "__self__", None)
    return self_obj if isinstance(self_obj, asyncio.Task) else None

def _task_from_handle(handle):
    return _task_from_callback(getattr(handle, "_callback", None))

def _snapshot_concurrent_tasks(exclude: Optional[asyncio.Task] = None):
    out = []
    for t in asyncio.all_tasks():
        if t is exclude or t.done():
            continue
        out.append(t)
    return out

@contextmanager
def PozPolicy():
    """Temporarily make asyncio create PozLoop()."""
    prev = asyncio.get_event_loop_policy()
    base = (asyncio.WindowsSelectorEventLoopPolicy
            if sys.platform == "win32"
            else asyncio.DefaultEventLoopPolicy)

    class _PozPolicy(base):  # type: ignore[misc]
        def new_event_loop(self):
            return PozLoop()

    asyncio.set_event_loop_policy(_PozPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(prev)

# ─────────────────────────────────────────────────────────────
# Public-only sleep shim so we can stretch timers cleanly
# ─────────────────────────────────────────────────────────────

_ORIG_SLEEP = asyncio.sleep

# Track: future -> (handle, deadline_time)
_SLEEP_FUT_STATE = {}
# Track: task -> its current sleep future (if any)
_TASK_SLEEP_FUT = weakref.WeakKeyDictionary()

async def _poz_sleep(delay, result=None):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    delay = max(0.0, float(delay))
    deadline = loop.time() + delay

    def _set():
        if not fut.done():
            fut.set_result(result)

    handle = loop.call_later(delay, _set)
    _SLEEP_FUT_STATE[fut] = (handle, deadline)

    task = asyncio.current_task()
    if task:
        _TASK_SLEEP_FUT[task] = fut

    try:
        return await fut
    finally:
        st = _SLEEP_FUT_STATE.pop(fut, None)
        if st is not None:
            h, _ = st
            try: h.cancel()
            except Exception: pass
        if task and _TASK_SLEEP_FUT.get(task) is fut:
            try: del _TASK_SLEEP_FUT[task]
            except Exception: pass

# Install shim
asyncio.sleep = _poz_sleep  # type: ignore[assignment]

def _extend_task_sleep(task: asyncio.Task, loop: asyncio.AbstractEventLoop, extra_ms: float) -> bool:
    """Extend this task's *current* sleep by extra_ms (ms). Returns True if extended."""
    if extra_ms <= 0: return False
    fut = _TASK_SLEEP_FUT.get(task)
    if fut is None or fut.done():
        return False
    st = _SLEEP_FUT_STATE.get(fut)
    if st is None:
        return False
    handle, deadline = st
    now = loop.time()
    remaining = max(0.0, deadline - now)
    try:
        handle.cancel()
    except Exception:
        pass
    new_deadline = now + remaining + (extra_ms / 1000.0)

    def _set():
        if not fut.done():
            fut.set_result(None)

    new_handle = loop.call_later(remaining + (extra_ms / 1000.0), _set)
    _SLEEP_FUT_STATE[fut] = (new_handle, new_deadline)
    return True

# ─────────────────────────────────────────────────────────────
# Endorser + proxy-from-task pattern for user-space primitives
# ─────────────────────────────────────────────────────────────

class PozEndorserMixin:
    """
    Carries not-before time (NB) in loop.time() units; consumed at handoff.
    Also registers each instance so virtual_speedup can apply tax to all live endorsers.
    """
    _poz_registry = weakref.WeakSet()

    def __init__(self):
        self._poz_nb = 0.0
        PozEndorserMixin._poz_registry.add(self)

    def poz_tax(self, delta_ms: float):
        loop = asyncio.get_running_loop()
        self._poz_nb = max(self._poz_nb, loop.time() + (delta_ms / 1000.0))

    def poz_not_before(self) -> float:
        return self._poz_nb

    def poz_consume(self):
        self._poz_nb = 0.0

def _poz_proxy_from_task(task: asyncio.Task, *, endorser, loop: asyncio.AbstractEventLoop):
    """
    Create a 'proxy' Future that completes with the same outcome as 'task',
    but not before endorser.poz_not_before(). Consumes the NB on completion.
    Cancelling the proxy cancels the task.
    """
    proxy = loop.create_future()

    def _complete():
        if proxy.done():
            return
        if task.cancelled():
            proxy.cancel()
        else:
            exc = task.exception()
            if exc is not None:
                proxy.set_exception(exc)
            else:
                proxy.set_result(task.result())
        consume = getattr(endorser, "poz_consume", None)
        if callable(consume):
            consume()

    def _task_done(_):
        now = loop.time()
        nb = float(getattr(endorser, "poz_not_before", lambda: 0.0)())
        remain = max(0.0, nb - now)
        if remain <= 0.0:
            _complete()
        else:
            loop.call_later(remain, _complete)

    task.add_done_callback(_task_done)

    def _proxy_done(p):
        if p.cancelled() and not task.done():
            task.cancel()
    proxy.add_done_callback(_proxy_done)

    return proxy

# Lock wrapper: delegate to original; delay completion via proxy
class PozLock(PozEndorserMixin, _ORIG_Lock):  # type: ignore[misc]
    def __init__(self, *a, **kw):
        _ORIG_Lock.__init__(self, *a, **kw)
        PozEndorserMixin.__init__(self)

    async def acquire(self):
        loop = asyncio.get_running_loop()
        inner_task = loop.create_task(_ORIG_Lock.acquire(self))  # call original
        proxy = _poz_proxy_from_task(inner_task, endorser=self, loop=loop)
        await proxy
        return True

# Semaphore wrappers
class PozSemaphore(PozEndorserMixin, _ORIG_Semaphore):  # type: ignore[misc]
    def __init__(self, *a, **kw):
        _ORIG_Semaphore.__init__(self, *a, **kw)
        PozEndorserMixin.__init__(self)

    async def acquire(self):
        loop = asyncio.get_running_loop()
        inner_task = loop.create_task(_ORIG_Semaphore.acquire(self))
        proxy = _poz_proxy_from_task(inner_task, endorser=self, loop=loop)
        await proxy
        return True

class PozBoundedSemaphore(PozSemaphore, _ORIG_BoundedSemaphore):  # type: ignore[misc]
    def __init__(self, *a, **kw):
        _ORIG_BoundedSemaphore.__init__(self, *a, **kw)
        PozSemaphore.__init__(self, *a, **kw)

# Queue (get/put may suspend): delegate and proxy
class PozQueue(PozEndorserMixin, _ORIG_Queue):  # type: ignore[misc]
    def __init__(self, *a, **kw):
        _ORIG_Queue.__init__(self, *a, **kw)
        PozEndorserMixin.__init__(self)

    async def get(self):
        loop = asyncio.get_running_loop()
        inner_task = loop.create_task(_ORIG_Queue.get(self))
        proxy = _poz_proxy_from_task(inner_task, endorser=self, loop=loop)
        return await proxy

    async def put(self, item):
        loop = asyncio.get_running_loop()
        inner_task = loop.create_task(_ORIG_Queue.put(self, item))
        proxy = _poz_proxy_from_task(inner_task, endorser=self, loop=loop)
        await proxy
        return None

def _patch_lock_sem_queue():
    asyncio.Lock = PozLock
    asyncio.Semaphore = PozSemaphore
    asyncio.BoundedSemaphore = PozBoundedSemaphore
    asyncio.Queue = PozQueue

_patch_lock_sem_queue()

# ─────────────────────────────────────────────────────────────
# Poz event loop (no private scheduling shims)
# ─────────────────────────────────────────────────────────────

class PozLoop(asyncio.SelectorEventLoop):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._poz_barrier: Optional[asyncio.Future] = None
        self._poz_timer = None
        self.poz_DELAY = None
        self.poz_site = None

        def _factory(loop_, coro):
            if _gate_bypass.get(False):
                return asyncio.Task(coro, loop=loop_)
            async def starter():
                barrier = getattr(loop_, "_poz_barrier", None)
                if barrier is not None:
                    await barrier
                return await coro
            return asyncio.Task(starter(), loop=loop_)

        self.set_task_factory(_factory)

    def _run_once(self):
        # vanilla run_once
        if self._ready:
            timeout = 0
        elif self._scheduled:
            timeout = max(0, self._scheduled[0]._when - self.time())
        else:
            timeout = None

        event_list = self._selector.select(timeout)
        self._process_events(event_list)

        now = self.time()
        while self._scheduled and self._scheduled[0]._when <= now:
            h = heapq.heappop(self._scheduled)
            if not getattr(h, "_cancelled", False):
                self._ready.append(h)

        ntodo = len(self._ready)
        for _ in range(ntodo):
            h = self._ready.popleft()
            if not getattr(h, "_cancelled", False):
                h._run()
            h = None  # drop ref

    def _virtual_speedup(self, delta: Optional[float] = None) -> None:
        self.poz_DELAY = float(delta)

        # identify calling file/line; enforce single site (your policy)
        get_key = _poz_callsite_key()
        if self.poz_site is None:
            self.poz_site = get_key
        else:
            assert get_key == self.poz_site, "Only one virtual speedup is allowed per experiment"

        t = asyncio.current_task(loop=self)
        if t is None:
            raise RuntimeError("virtual_speedup() must be called from within a Task")

        _set_target(t)

        # For each OTHER task:
        #  - If suspended on *our* sleep → extend timer by Δ
        for other in (x for x in asyncio.all_tasks(loop=self) if x is not t and not x.done()):
            _extend_task_sleep(other, self, self.poz_DELAY * 1000.0)

        # Raise NB on all known endorsers (locks/semaphores/queues)
        for endorser in list(PozEndorserMixin._poz_registry):
            try:
                endorser.poz_tax(self.poz_DELAY * 1000.0)
            except RuntimeError:
                pass
            except Exception:
                pass

        # Arm / extend the gate that holds NEW non-bypass tasks
        barrier = _extend_gate(self, self.poz_DELAY)
        barrier.add_done_callback(lambda _f: _cleanup_gate(self, barrier))

        # let this task (and anything it spawns) bypass the gate
        _gate_bypass.set(True)

        # When this task finishes, clear target
        def _on_done(_fut):
            if _get_target() is t:
                _set_target(None)
        t.add_done_callback(_on_done)

    @classmethod
    async def virtual_speedup(cls, delta = 1.0):
        loop = asyncio.get_running_loop()
        if not isinstance(loop, cls):
            raise RuntimeError("virtual_speedup_here: running loop is not a PozLoop")
        loop._virtual_speedup(delta)
        await asyncio.sleep(0) # allow rescheduling to happen *now*

# ─────────────────────────────────────────────────────────────
# Gate helpers (unchanged)
# ─────────────────────────────────────────────────────────────

def _arm_gate(loop: "PozLoop", delta: float) -> asyncio.Future:
    """Arm the gate for delta seconds if not already armed; return the barrier Future."""
    if loop._poz_barrier is None or loop._poz_barrier.done():
        barrier = loop.create_future()
        timer = loop.call_later(delta, barrier.set_result, None)  # open after Δ
        loop._poz_barrier = barrier
        loop._poz_timer = timer
    return loop._poz_barrier  # type: ignore

def _cleanup_gate(loop: "PozLoop", barrier: asyncio.Future) -> None:
    """Clear loop references once THIS barrier opens."""
    if getattr(loop, "_poz_barrier", None) is barrier:
        loop._poz_barrier = None
        t = getattr(loop, "_poz_timer", None)
        if t is not None:
            try: t.cancel()
            except Exception: pass
        loop._poz_timer = None

def _extend_gate(loop: "PozLoop", delta: float) -> asyncio.Future:
    """If a gate is active, extend its deadline to 'now + delta'."""
    if loop._poz_barrier is None or loop._poz_barrier.done():
        return _arm_gate(loop, delta)
    try:
        loop._poz_timer.cancel()
    except Exception:
        pass
    loop._poz_timer = loop.call_later(delta, loop._poz_barrier.set_result, None)
    return loop._poz_barrier
