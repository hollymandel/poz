import asyncio
import collections
import functools
import heapq
import weakref
import contextvars
from typing import Optional


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
            async def starter():
                # If a gate is armed when this task takes its first step, wait for it.
                barrier = getattr(loop_, "_poz_barrier", None)
                if barrier is not None:
                    await barrier
                return await coro
            return asyncio.Task(starter(), loop=loop_)

        self.set_task_factory(_factory)

    # Intercept resumes WHEN THEY ARE SCHEDULED.
    def call_soon(self, callback, *args, context=None):
        t = _task_from_callback(callback)
        target = _get_target()
        if t is not None and t in PENALIZE_ONCE and t is not target:
            # divert this FIRST resume into the near future, once
            handle = super().call_later(self.poz_DELAY, callback, *args, context=context)
            # consume the one-shot mark
            try:
                del PENALIZE_ONCE[t]
            except KeyError:
                pass
            print(f"[poz] one-shot delay via call_soon: task={id(t)} delay={self.poz_DELAY}")
            return handle
        return super().call_soon(callback, *args, context=context)

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

def _arm_gate(loop: "PozLoop", delta: float) -> asyncio.Future:
    # Arm only if no active barrier; otherwise leave the current one.
    if loop._poz_barrier is None or loop._poz_barrier.done():
        barrier = loop.create_future()
        timer = loop.call_later(delta, barrier.set_result, None)  # opens gate after Δ
        loop._poz_barrier = barrier
        loop._poz_timer = timer
    return loop._poz_barrier  # type: ignore

def _cleanup_gate(loop: "PozLoop", barrier: asyncio.Future) -> None:
    """Clear pointers once THIS barrier has opened."""
    if getattr(loop, "_poz_barrier", None) is barrier:
        # timer has fired (or will be no-op); clear references
        loop._poz_barrier = None
        t = getattr(loop, "_poz_timer", None)
        if t is not None:
            try: t.cancel()
            except Exception: pass
        loop._poz_timer = None


def poz_target(func):
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

        # (A) one-shot delay for tasks already "holding"
        holding_others = _snapshot_holding_tasks(exclude=this)
        for t in holding_others:
            PENALIZE_ONCE[t] = True
        print(f"[poz] snapshot holding tasks: {len(holding_others)} to delay once")

        # (B) gate the LAUNCH of new tasks for poz_DELAY
        barrier = _arm_gate(loop, loop.poz_DELAY)
        barrier.add_done_callback(lambda _f: _cleanup_gate(loop, barrier))

        try:
            return await func(*args, **kwargs)
        finally:
            _poz_flag.reset(token)
            if _get_target() is this:
                _set_target(None)
            PENALIZE_ONCE.clear()
    return wrapper