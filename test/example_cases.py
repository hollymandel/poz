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
try:
    from poz_proto import PozLoop  # type: ignore
except Exception as e:  # pragma: no cover - informative failure
    pytest.skip(f"poz_proto.PozLoop not importable: {e}", allow_module_level=True)

try:
    from poz_proto import PozPolicy  # type: ignore
except Exception:
    PozPolicy = None  # optional


# ---- Helpers ------------------------------------------------------------------

def run_with_poz(coro: "asyncio.Future"):
    """Run a coroutine to completion on a fresh PozLoop, then flush residual tasks."""
    loop = PozLoop()
    try:
        async def runner():
            # run the actual test coroutine
            result = await coro
            # drain the loop a few turns to flush follow-on callbacks
            # await drain_loop(3)
            return result
        return loop.run_until_complete(runner())
    finally:
        loop.close()

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
@pytest.mark.timeout(10)
def test_cpu_bound_virtual_speedup_has_no_effect_on_cpu_runtime():
    async def process_a(i):
        print(f"A{i} starting CPU work")
        t0 = time.perf_counter()
        cpu_burn_ms(1500)  # ~0.15s
        print(f"A{i} done, elapsed {(time.perf_counter()-t0):0.3f}s")

    async def process_b(i, with_speedup: bool):
        print(f"B{i} starting CPU work")
        t0 = time.perf_counter()
        if with_speedup:
            # No-op effect on CPU-only sections by design
            await PozLoop.virtual_speedup(1)  
        cpu_burn_ms(1500)  # ~0.15s
        print(f"B{i} done, elapsed {(time.perf_counter()-t0):0.3f}s")

    async def run_pair(with_speedup: bool) -> float:
        t0 = time.perf_counter()
        await asyncio.gather(process_a(0), process_b(0, with_speedup))
        return time.perf_counter() - t0

    # Run without speedup then with speedup
    t_baseline = run_with_poz(run_pair(False))
    t_speedup  = run_with_poz(run_pair(True))

    # Expect similar durations; allow generous 35% tolerance to reduce flakiness
    ratio = t_speedup / t_baseline if t_baseline > 1e-6 else -1
    print(f"baseline={t_baseline:.3f}s speedup={t_speedup:.3f}s ratio={ratio:.2f}")
    # If virtual speedup affected CPU time, we'd see a large systematic shift.
    assert 0.95 <= ratio <= 1.05


# 2) lock_competition.py → two coroutines contend for a lock. Speeding the lock-holding
#     coro should have an effect, speeding the suspended coro should not.
@pytest.mark.timeout(60)
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
@pytest.mark.timeout(5)
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
    
    
# # 4) rate_limiting_example.py → A triggers B; verify causality (B after A completion).
# @pytest.mark.timeout(3)
# def test_rate_limiting_trigger_after_A(capfd):
#     async def process_b(i):
#         await asyncio.sleep(0)
#         print(f"B{i} start")
#         await asyncio.sleep(0.02)
#         print(f"B{i} done")

#     async def process_a(i):
#         print(f"A{i} start")
#         PozLoop.virtual_speedup(0.02)
#         await asyncio.sleep(0.02)
#         print(f"A{i} done — kicking off B{i}")
#         await process_b(i)

#     async def main():
#         await process_a(0)

#     run_with_poz(main())
#     out, _ = capfd.readouterr()
#     lines = [ln.strip() for ln in out.splitlines()]
#     # Ensure A announces completion before B starts.
#     a_done_idx = next(i for i, ln in enumerate(lines) if "A0 done — kicking off B0" in ln)
#     b_start_idx = next(i for i, ln in enumerate(lines) if "B0 start" in ln)
#     assert a_done_idx < b_start_idx


# # 5) suspended_thread.py → F applies virtual speedup during CPU work; verify it does not
# #    reorder G/H lock acquisition. H should only acquire after G releases.
# @pytest.mark.timeout(5)
# def test_suspended_thread_virtual_speedup_no_effect_on_lock_order(capfd):
#     lock = asyncio.Lock()

#     async def F():
#         await asyncio.sleep(0.01)
#         PozLoop.virtual_speedup(0.05)
#         print("F CPU start")
#         cpu_burn_ms(80)
#         print("F CPU end")

#     async def G():
#         async with lock:
#             print("G acquired lock")
#             await asyncio.sleep(0.05)
#             print("G released lock")
#         await asyncio.sleep(0)  # give H a chance next

#         print("G CPU start")
#         cpu_burn_ms(60)
#         print("G CPU end")

#     async def H():
#         print("H will await")
#         await asyncio.sleep(0)  # ensure G likely acquires first
#         async with lock:
#             print("H acquired lock")
#             await asyncio.sleep(0.05)
#             print("H released lock")

#     async def main():
#         await asyncio.gather(F(), G(), H())

#     run_with_poz(main())
#     out, _ = capfd.readouterr()
#     lines = [ln.strip() for ln in out.splitlines()]

#     g_acq = lines.index("G acquired lock")
#     g_rel = lines.index("G released lock")
#     h_acq = lines.index("H acquired lock")
#     # Assert that H acquires *after* G releases, i.e., no reordering effect.
#     assert g_acq < g_rel < h_acq

