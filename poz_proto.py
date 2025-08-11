import asyncio
import collections
import functools
import heapq
import weakref
import contextvars
from typing import Optional, Dict, List
import time
import sys
from poz_logger import (
    PozOwnerMixin,
    CURRENT_OWNER_TID,
    describe_ready_deque,
    describe_scheduled_heap,
    describe_event_list,
    describe_task_list,
)

# Avoid proactor loop on windows machine
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_TARGET_REF: Optional[weakref.ReferenceType[asyncio.Task]]  = None

def _get_target():
    return _TARGET_REF() if _TARGET_REF else None
def _set_target(t: Optional[asyncio.Task]):
    global _TARGET_REF
    _TARGET_REF = weakref.ref(t) if t is not None else None

# Map of tasks that should be delayed exactly once → True
# WeakKeyDictionary so tasks don’t get kept alive.
PENALIZE_ONCE = weakref.WeakKeyDictionary()

_poz_flag = contextvars.ContextVar("poz_flag", default=False)
_gate_armed_event = asyncio.Event()
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
        # Include all tasks that aren't done — these are "live"
        out.append(t)
    return out

def _count_tasks():
    return 1#len(asyncio.all_tasks())

class PozLoopLogEvent():
    def __init__(self, data: Dict):
        self.data = data

class PozLoopLog():
    def __init__(self):
        self.data = []

    def append(self, event: PozLoopLogEvent):
        self.data.append(event)

class PozLoop(asyncio.SelectorEventLoop):
    def __init__(self, delay=1.0, record=False, *a, **k):
        super().__init__(*a, **k)
        self._poz_barrier: Optional[asyncio.Future] = None
        self._poz_timer = None
        self.poz_DELAY = delay
        self.record = record
        self._poz_task_index = {}
        
        if record:
            self.log = PozLoopLog()

        def _factory(loop_, coro):
            if _gate_bypass.get(False):
                return asyncio.Task(coro, loop=loop_)

            async def starter():
                barrier = getattr(loop_, "_poz_barrier", None)

                # If this task is already in PENALIZE_ONCE, don’t gate it —
                # it will pay its one-shot delay later via the shim.
                myself = asyncio.current_task()
                if barrier is not None and myself not in PENALIZE_ONCE:
                    await barrier

                return await coro

            return asyncio.Task(starter(), loop=loop_)

    # Safety net: if any marked task is already in ready, delay it once and consume.
    def _poz_sweep_ready_once(self):
        if not getattr(self, "_ready", None) or not PENALIZE_ONCE:
            return
        new_ready = collections.deque()
        while self._ready:
            h = self._ready.popleft()
            if getattr(h, "_cancelled", False):
                continue
            t = _task_from_handle(h)
            target = _get_target()
            if t is not None and t in PENALIZE_ONCE and t is not target:
                super().call_later(self.poz_DELAY/_count_tasks(), h._run)
                try:
                    del PENALIZE_ONCE[t]
                except KeyError:
                    pass
                print(f"[poz] one-shot delay from ready: task={id(t)} delay={self.poz_DELAY/_count_tasks()}")
            else:
                new_ready.append(h)
        self._ready = new_ready

    def _run_once(self):
        record_dict = {} if getattr(self, "record", False) else None

        # --- timeout selection ---
        if self._ready:
            timeout = 0
            if record_dict is not None:
                record_dict["ready"] = describe_ready_deque(self._ready, self._poz_task_index, limit=200)
        elif self._scheduled:
            timeout = max(0, self._scheduled[0]._when - self.time())
        else:
            timeout = None

        # --- poll I/O ---
        event_list = self._selector.select(timeout)
        if record_dict is not None:
            record_dict["event_list"] = describe_event_list(event_list, limit=500)

        self._process_events(event_list)

        # --- move due timers -> ready ---
        now = self.time()
        if record_dict is not None:
            record_dict["scheduled"] = describe_scheduled_heap(self._scheduled, self._poz_task_index, limit=500)
        while self._scheduled and self._scheduled[0]._when <= now:
            h = heapq.heappop(self._scheduled)
            if not getattr(h, "_cancelled", False):
                self._ready.append(h)

        # --- remove to-pause tasks from ready ---
        self._poz_sweep_ready_once()
        if record_dict is not None:
            record_dict["new_ready"] = describe_ready_deque(self._ready, self._poz_task_index, limit=200)

        # --- run ready snapshot ---
        ntodo = len(self._ready)
        for _ in range(ntodo):
            h = self._ready.popleft()
            if not getattr(h, "_cancelled", False):
                # ensure callbacks run under their stamped owner context
                owner = getattr(h, "_poz_owner_tid", None)
                tok = None
                if owner is not None:
                    tok = CURRENT_OWNER_TID.set(owner)
                try:
                    h._run()
                finally:
                    if tok is not None:
                        CURRENT_OWNER_TID.reset(tok)
            h = None

        if record_dict is not None:
            try:
                record_dict["tasks"] = describe_task_list(asyncio.all_tasks())
            except Exception:
                pass
            self.log.append(PozLoopLogEvent(record_dict))
            print(record_dict)
        
    def run_until_complete(self, *args, **kwargs):
        start_time = time.time()
        outputs = super().run_until_complete(*args, **kwargs)
        print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")
        return outputs

    def _virtual_speedup(self, delta: Optional[float] = None) -> None:
        t = asyncio.current_task(loop=self)
        if t is None:
            raise RuntimeError("virtual_speedup() must be called from within a Task")

        _set_target(t)                       # <──  tell the shims who the target is

        # default delta
        d = self.poz_DELAY if delta is None else float(delta)

        # 1) mark all *other* live tasks so their *next* state-affecting action is delayed
        for other in _snapshot_concurrent_tasks(exclude=t):
            PENALIZE_ONCE[other] = True

        # 2) arm / extend the gate that holds NEW non-bypass tasks
        barrier = _extend_gate(self, d)
        barrier.add_done_callback(lambda _f: _cleanup_gate(self, barrier))

        # 3) let this task (and anything it spawns) bypass the gate
        _gate_bypass.set(True)

        # 4) when this task finishes, clear target + bypass
        def _on_done(_fut):
            if _get_target() is t:
                _set_target(None)
        t.add_done_callback(_on_done)

        def _on_done(_fut):
            PENALIZE_ONCE.clear()          # ← nothing left to delay
            if _get_target() is t:
                _set_target(None)

        t.add_done_callback(_on_done)     

    @classmethod
    def virtual_speedup(cls, delta = None):
        loop = asyncio.get_running_loop()
        if not isinstance(loop, cls):
            raise RuntimeError("virtual_speedup_here: running loop is not a PozLoop")
        loop._virtual_speedup(delta)

def _should_delay(task):
    return task in PENALIZE_ONCE and task is not _get_target()

# ── 1.  Scheduling  ───────────────────────────────────────────

_orig_call_soon = asyncio.BaseEventLoop._call_soon
def _call_soon_shim(self, cb, *a, context=None, **kw):
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        return self.call_later(self.poz_DELAY/_count_tasks(), cb, *a, context=context, **kw)
    return _orig_call_soon(self, cb, *a, **kw)
asyncio.BaseEventLoop._call_soon = _call_soon_shim

_orig_call_soon_ts = asyncio.BaseEventLoop.call_soon_threadsafe
def _call_soon_ts_shim(self, cb, *a, **kw):
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        return self.call_later(self.poz_DELAY/_count_tasks(), cb, *a, **kw)
    return _orig_call_soon_ts(self, cb, *a, **kw)
asyncio.BaseEventLoop.call_soon_threadsafe = _call_soon_ts_shim

_orig_run_in_exec = asyncio.BaseEventLoop.run_in_executor
async def _run_in_exec_shim(self, exec_, func, *a):
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        await asyncio.sleep(self.poz_DELAY/_count_tasks())
    return await _orig_run_in_exec(self, exec_, func, *a)
asyncio.BaseEventLoop.run_in_executor = _run_in_exec_shim

# ── 2.  Lock  / Semaphore  / Queue  ──────────────────────────

def _wrap_acquire(cls):
    orig = cls.acquire
    async def acquire(self, *a, **k):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            await asyncio.sleep(self._loop.poz_DELAY/_count_tasks())
        return await orig(self, *a, **k)
    cls.acquire = acquire
    return cls

def _wrap_release(cls):
    orig = cls.release
    def release(self, *a, **k):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            self._loop.call_later(self._loop.poz_DELAY, orig, self, *a, **k)
        else:
            orig(self, *a, **k)
    cls.release = release
    return cls

def _patch_lock_sem_queue():
    # Lock
    asyncio.Lock = _wrap_release(_wrap_acquire(asyncio.Lock))

    # Semaphore
    asyncio.Semaphore = _wrap_release(_wrap_acquire(asyncio.Semaphore))

    # Queue (put / get)
    q_cls = asyncio.Queue
    orig_put = q_cls.put
    orig_get = q_cls.get

    async def put(self, item):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            await asyncio.sleep(self._loop.poz_DELAY/_count_tasks())
        return await orig_put(self, item)
    async def get(self):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            await asyncio.sleep(self._loop.poz_DELAY/_count_tasks())
        return await orig_get(self)

    q_cls.put, q_cls.get = put, get

_patch_lock_sem_queue()

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
    # reschedule the timer later if needed
    try:
        remaining = getattr(loop._poz_timer, "_when", None)
    except Exception:
        remaining = None
    # Cancel old timer and set a new one at now+delta if we can’t compare.
    try:
        loop._poz_timer.cancel()
    except Exception:
        pass
    loop._poz_timer = loop.call_later(delta, loop._poz_barrier.set_result, None)
    return loop._poz_barrier
