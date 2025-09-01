from pathlib import Path
import textwrap


import asyncio
import time
import sys
import types
import inspect
import contextlib
from typing import List, Tuple, Callable, Optional

import pytest

import sys
sys.path.append("/Users/hollymandel/poz")

import poz


# ---- Helpers ------------------------------------------------------------------

def run_with_poz(coro: "asyncio.Future"):
    """Run a coroutine to completion on a fresh PozLoop, then flush residual tasks."""
    with poz.poz_context():
        return asyncio.run(coro)

# async def flush_ready_once():
#     ev = asyncio.Event()
#     loop = asyncio.get_running_loop()
#     loop.call_soon(ev.set)
#     await ev.wait()

# async def drain_loop(max_turns: int = 5):
#     for _ in range(max_turns):
#         await flush_ready_once()
#         await asyncio.sleep(0)

def cpu_burn_ms(ms: int):
    """Light CPU burn to simulate CPU-bound work without taking long."""
    end = time.perf_counter() + (ms / 1000.0)
    n = 1000
    s = 0
    while time.perf_counter() < end:
        # A small tight loop doing arithmetic to hold the interpreter/GIL.
        s += sum(i * i for i in range(n))
    return s

def timer_print(message, start):
    print(f"[{time.time()-start:0.3f}] {message}")

@contextlib.asynccontextmanager
async def acquired(lock: asyncio.Lock):
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def order_from_lines(lines: List[str], prefixes: Tuple[str, ...]) -> List[str]:
    """Extract an event sequence from captured output lines by prefix match."""
    out = []
    for ln in lines:
        for p in prefixes:
            if ln.startswith(p):
                out.append(p)
                break
    return out


# ---- Tests --------------------------------------------------------------------

# 1) CPU_bound.py → virtual speedup should not affect runtime for CPU-bound sections.
#    We compare elapsed times of two back-to-back runs with and without virtual_speedup
#    and require them to be within a modest ratio (not necessarily equal).
def test_cpu_bound_virtual_speedup_has_no_effect_on_cpu_runtime():
    async def process_a(i):
        print(f"A{i} starting CPU work")
        t0 = time.perf_counter()
        cpu_burn_ms(1500)  # ~0.15s
        print(f"A{i} done, elapsed {(time.perf_counter()-t0):0.3f}s")

    async def process_b(i, with_speedup: bool):
        await asyncio.sleep(0) # ensure A scheduled first
        print(f"B{i} starting CPU work")
        t0 = time.perf_counter()
        if with_speedup:
            # No-op effect on CPU-only sections by design
            poz.virtual_speedup(1)  
            await asyncio.sleep(0)
            print(dict(poz.delayable_handle._poz_ledger))
        cpu_burn_ms(1500)  # ~0.15s
        print(f"B{i} done, elapsed {(time.perf_counter()-t0):0.3f}s")

    async def run_pair(with_speedup: bool) -> float:
        t0 = time.perf_counter()
        await asyncio.gather(process_a(0), process_b(0, with_speedup))
        return time.perf_counter() - t0

    # Run without speedup then with speedup
    t_baseline = run_with_poz(run_pair(False))
    print("\n\n")
    t_speedup  = run_with_poz(run_pair(True))

    # Expect similar durations; allow generous 35% tolerance to reduce flakiness
    ratio = t_speedup / t_baseline if t_baseline > 1e-6 else -1
    print(f"baseline={t_baseline:.3f}s speedup={t_speedup:.3f}s ratio={ratio:.2f}")
    # If virtual speedup affected CPU time, we'd see a large systematic shift.
    assert 0.95 <= ratio <= 1.05


# 2) lock_competition.py → two coroutines contend for a lock. Speeding the lock-holding
#     coro should have an effect, speeding the suspended coro should not.
def test_lock_competition():
    def _run_once(f_speedup: bool, g_speedup: bool) -> float:
        async def main():
            # Create the lock inside the running PozLoop so it's bound correctly
            lock = asyncio.Lock()

            async def F():
                await asyncio.sleep(0)
                async with lock:
                    print("F has the lock")
                    if f_speedup:
                        await PozLoop.virtual_speedup(1.0)
                    cpu_burn_ms(1000)
                    await asyncio.sleep(0)

            async def G():
                await asyncio.sleep(0)
                print("G launched")
                if g_speedup:
                    await PozLoop.virtual_speedup(1.0)
                async with lock:
                    print("G has the lock")
                    cpu_burn_ms(1000)
                    await asyncio.sleep(0)

            t0 = time.perf_counter()
            await asyncio.gather(F(), G())
            return time.perf_counter() - t0

        return run_with_poz(main())

    t_no_speedup = _run_once(False, False)
    t_f_speedup  = _run_once(True,  False)
    t_g_speedup  = _run_once(False, True)

    f_speedup_ratio = t_f_speedup / t_no_speedup if t_no_speedup > 1e-6 else -1
    g_speedup_ratio = t_g_speedup / (t_no_speedup+1) if t_no_speedup > 1e-6 else -1

    assert 0.95 < f_speedup_ratio < 1.05
    assert 0.95 < g_speedup_ratio < 1.05


# 3) paradoxical_slowdown.py → virtual speedup in B changes effective order: ABAB → ABBA, 
#       increasing overall runtime
def test_paradoxical_slowdown():
    async def process_a(i, start_time, lock):
        if i == 2:
            return
        await asyncio.sleep(0)
        async with lock:
            print(f"[{time.perf_counter()-start_time:0.3f}] A{i} has the lock")
            await asyncio.sleep(.1)
        print(f"[{time.perf_counter()-start_time:0.3f}] A{i} released the lock")
        await asyncio.sleep(.5)
        await process_a(i+1, start_time, lock)

    async def process_b(i, start_time, lock, with_speedup = False):
        if i == 2:
            return
        await asyncio.sleep(0)
        print(f"[{time.perf_counter()-start_time:0.3f}] B{i} speedup")
        if with_speedup:
            print("speedup!")
            await PozLoop.virtual_speedup(0.3)  
        await asyncio.sleep(.3)
        async with lock:
            print(f"[{time.perf_counter()-start_time:0.3f}] B{i} has the lock")
            await asyncio.sleep(0.3)
        print(f"[{time.perf_counter()-start_time:0.3f}] B{i} released the lock")
        await process_b(i+1, start_time, lock, with_speedup)

    async def main(with_speedup):
        start = time.perf_counter()
        lock = asyncio.Lock()
        a0 = asyncio.create_task(process_a(0, start, lock))
        b0 = asyncio.create_task(process_b(0, start, lock, with_speedup))
        await asyncio.gather(a0, b0)
        return time.perf_counter() - start

    dt_no_speedup = run_with_poz(main(False))
    dt_speedup = run_with_poz(main(True))
    
    assert 0.95 < (dt_no_speedup/1.2) < 1.05
    assert 0.95 < (dt_speedup/1.9) < 1.05
    
    
# 4) rate_limiting_example.py: 
def test_rate_limiting():
    async def process_a(i, a_speedup=False, b_speedup=False, start=None):
        if start==None:
            start = time.time()
        if i == 3:
            return
        print(f"[{time.time()-start:0.4f}] A{i} start")
        print(f"[{time.time()-start:0.4f}] Launching B{i}")
        asyncio.create_task(process_b(i, b_speedup, start))
        await asyncio.sleep(0)
        if a_speedup:
            await PozLoop.virtual_speedup(0.1)
        print(f"[{time.time()-start:0.4f}] awaiting A")
        await asyncio.sleep(0.2)
        print(f"[{time.time()-start:0.4f}] A{i} done")
        return start

    async def process_b(i, b_speedup, start):
        print(f"[{time.time()-start:0.4f}] B{i} start")
        if b_speedup:
            print(f"[{time.time()-start:0.4f}] B{i} speedup")
            await PozLoop.virtual_speedup(0.1)
        await asyncio.sleep(0.1)
        print(f"[{time.time()-start:0.4f}] B{i} done")

    async def main(speedup_a, speedup_b):
        start = time.perf_counter()
        start2 = time.time()
        for i in range(3):
            await process_a(i, speedup_a, speedup_b, start2)
        return  time.perf_counter() - start

    no_speedup_dt = run_with_poz(main(False, False))
    a_speedup_dt = run_with_poz(main(True, False))
    b_speedup_dt = run_with_poz(main(False, True))

    a_speedup_ratio = a_speedup_dt / (no_speedup_dt+0)
    b_speedup_ratio = b_speedup_dt / (no_speedup_dt+0.3)

    assert 0.95 < a_speedup_ratio < 1.05
    assert 0.95 < b_speedup_ratio < 1.05


# 5) suspended_thread.py - suspended threads G and H unaffected by speedup that takes less time
# than G's wait time. H is the suspended thread, confirm no double-counting of pause.
def test_suspended_thread():
    async def F(speedup, start):
        await asyncio.sleep(0.001) # let F and G start
        if speedup:
            timer_print("Poz speedup of F", start)
            await PozLoop.virtual_speedup(0.1)

        timer_print("F CPU start", start)
        cpu_burn_ms(200)

        timer_print("F CPU end", start)

    async def G(lock, start):
        async with lock:
            timer_print("G acquired lock, starting sleep", start)
            await asyncio.sleep(0.2)
            timer_print("G released lock", start)

        await asyncio.sleep(0.001)  # give H a chance next

        timer_print("G CPU start", start)
        cpu_burn_ms(120)
        timer_print("G CPU end", start)

    async def H(lock, start):
        await asyncio.sleep(0)  # ensure G likely acquires first
        async with lock:
            timer_print("H acquired lock, starting sleep", start)
            await asyncio.sleep(0.2)
            timer_print("H released lock", start)

    async def main(speedup_f):
        start = time.perf_counter()
        start2 = time.time()
        lock = asyncio.Lock()
        await asyncio.gather(F(speedup_f, start2), G(lock, start2), H(lock, start2))
        return  time.perf_counter() - start

    print("\n")
    with_speedup = run_with_poz(main(True))
    print('\n')
    without_speedup = run_with_poz(main(False))

    print(with_speedup)
    print(without_speedup)
    
    ratio = with_speedup / (without_speedup + .1)
    assert 0.95 < ratio < 1.05


