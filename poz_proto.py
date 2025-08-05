import asyncio
import collections
import functools
import heapq
import weakref
import contextvars
from typing import Optional
import time

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

def _snapshot_holding_tasks(exclude: Optional[asyncio.Task]):
    """Return tasks currently 'holding' (awaiting a Future), excluding 'exclude'."""
    out = []
    for t in asyncio.all_tasks():
        if t is exclude or t.done():
            continue
        # Heuristic: in CPython asyncio, a waiting Task has a private _fut_waiter
        waiter = getattr(t, "_fut_waiter", None)
        if waiter is not None and not waiter.done():
            out.append(t)
    return out

class PozLoop(asyncio.SelectorEventLoop):
    def __init__(self, delay=1.0, *a, **k):
        super().__init__(*a, **k)
        self._poz_barrier: Optional[asyncio.Future] = None
        self._poz_timer = None
        self.poz_DELAY = delay

        def _factory(loop_, coro):
            # If caller is in a bypass context, don't gate
            if _gate_bypass.get(False):
                return asyncio.Task(coro, loop=loop_)
            # Otherwise, gate the task *if* a barrier is armed at first step
            async def starter():
                barrier = getattr(loop_, "_poz_barrier", None)
                if barrier is not None:
                    await barrier
                return await coro
            return asyncio.Task(starter(), loop=loop_)

        self.set_task_factory(_factory)

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
                super().call_later(self.poz_DELAY, h._run)
                try:
                    del PENALIZE_ONCE[t]
                except KeyError:
                    pass
                print(f"[poz] one-shot delay from ready: task={id(t)} delay={self.poz_DELAY}")
            else:
                new_ready.append(h)
        self._ready = new_ready

    def _run_once(self):
        # --- timeout selection (standard) ---
        if self._ready:
            timeout = 0
        elif self._scheduled:
            timeout = max(0, self._scheduled[0]._when - self.time())
        else:
            timeout = None

        # --- poll I/O ---
        event_list = self._selector.select(timeout)
        self._process_events(event_list)

        # --- move due timers -> ready ---
        now = self.time()
        while self._scheduled and self._scheduled[0]._when <= now:
            h = heapq.heappop(self._scheduled)
            if not getattr(h, "_cancelled", False):
                self._ready.append(h)

        # --- our one-shot sweep (only affects marked tasks) ---
        self._poz_sweep_ready_once()

        # --- run ready snapshot ---
        ntodo = len(self._ready)
        for _ in range(ntodo):
            h = self._ready.popleft()
            if not getattr(h, "_cancelled", False):
                h._run()
            h = None  # drop ref

    def run_until_complete(self, *args, **kwargs):
        start_time = time.time()
        outputs = super().run_until_complete(*args, **kwargs)
        print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")
        return outputs

def _should_delay(task):
    return task in PENALIZE_ONCE and task is not _get_target()

# ── 1.  Scheduling  ───────────────────────────────────────────

_orig_call_soon = asyncio.BaseEventLoop._call_soon
def _call_soon_shim(self, cb, *a, context=None, **kw):
    print("second one")
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        return self.call_later(self.poz_DELAY, cb, *a, context=context, **kw)
    return _orig_call_soon(self, cb, *a, **kw)
asyncio.BaseEventLoop._call_soon = _call_soon_shim

_orig_call_soon_ts = asyncio.BaseEventLoop.call_soon_threadsafe
def _call_soon_ts_shim(self, cb, *a, **kw):
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        return self.call_later(self.poz_DELAY, cb, *a, **kw)
    return _orig_call_soon_ts(self, cb, *a, **kw)
asyncio.BaseEventLoop.call_soon_threadsafe = _call_soon_ts_shim

_orig_run_in_exec = asyncio.BaseEventLoop.run_in_executor
async def _run_in_exec_shim(self, exec_, func, *a):
    task = asyncio.current_task(loop=self)
    if task and _should_delay(task):
        PENALIZE_ONCE.discard(task)
        await asyncio.sleep(self.poz_DELAY)
    return await _orig_run_in_exec(self, exec_, func, *a)
asyncio.BaseEventLoop.run_in_executor = _run_in_exec_shim

# ── 2.  Lock  / Semaphore  / Queue  ──────────────────────────

def _wrap_acquire(cls):
    orig = cls.acquire
    async def acquire(self, *a, **k):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            await asyncio.sleep(self._loop.poz_DELAY)
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
            await asyncio.sleep(self._loop.poz_DELAY)
        return await orig_put(self, item)
    async def get(self):
        task = asyncio.current_task()
        if task and _should_delay(task):
            PENALIZE_ONCE.discard(task)
            await asyncio.sleep(self._loop.poz_DELAY)
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

def poz_target(func):
    """When the target starts:
       - delay next resume of tasks already holding (your PENALIZE_ONCE),
       - arm a gate for poz_DELAY seconds that blocks NEW non-bypass tasks,
       - allow tasks created by the target (and its subtasks) to bypass the gate.
    """
    if not asyncio.iscoroutinefunction(func):
        raise TypeError("@poz_target must decorate an async def")

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        token = _poz_flag.set(True)
        this = asyncio.current_task()
        if this is None:
            raise RuntimeError("@poz_target must run inside a Task")
        _set_target(this)

        loop: PozLoop = asyncio.get_running_loop()  # type: ignore

        # (A) one-shot delay for tasks that are already "holding"
        holding_others = _snapshot_holding_tasks(exclude=this)
        for t in holding_others:
            PENALIZE_ONCE[t] = True
        print(f"[poz] snapshot holding tasks: {len(holding_others)} to delay once")

        # (B) Arm the gate for Δ seconds (it stays up even if target finishes early)
        barrier = _arm_gate(loop, loop.poz_DELAY)
        barrier.add_done_callback(lambda _f: _cleanup_gate(loop, barrier))

        # (C) Allow the target (and anything it spawns) to bypass the gate
        bypass_tok = _gate_bypass.set(True)
        try:
            return await func(*args, **kwargs)
        finally:
            _gate_bypass.reset(bypass_tok)   # new tasks outside target won't bypass
            _poz_flag.reset(token)
            if _get_target() is this:
                _set_target(None)
            PENALIZE_ONCE.clear()
            # NOTE: do NOT close the barrier here; timer will open it at Δ
    return wrapper
