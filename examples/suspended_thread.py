import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop
import time 

async def F():
    await asyncio.sleep(0.1)

    delta = 30
    print(f"virtual speedup in F CPU work of size {delta}")
    PozLoop.virtual_speedup(delta)

    print("F started CPU work")
    sum(i**2 for i in range(10_000_000))  # approx 3s of CPU work
    print("F finished CPU work")

async def G(lock):
    async with lock:
        print("G acquired lock")
        await asyncio.sleep(2)
        print("G released lock")

    # yield to allow H to acquire the lock, otherwise CPU work will be done first.
    # But the behavior of Poz is valid (virtual speedup has no effect) in either case.
    await asyncio.sleep(0)

    print("G started CPU work")
    sum(i**2 for i in range(10_000_000))  # approx 3s of CPU work
    print("G finished CPU work")


async def H(lock):
    print("H will await")
    await asyncio.sleep(0) # make sure G starts out with the lock
    async with lock:
        print("H acquired lock")
        await asyncio.sleep(2)
        print("H released lock")



def main():
    loop = PozLoop(delay=5.0)
    asyncio.set_event_loop(loop)
    lock = asyncio.Lock()

    async def _runner():
        f = asyncio.create_task(F())
        g = asyncio.create_task(G(lock))
        h = asyncio.create_task(H(lock))
        await asyncio.gather(f,g,h)

    loop.run_until_complete(_runner())
    loop.close()

if __name__ == "__main__":
    try:
        # start = time.time()
        main()
        # print(f"Elapsed time: {time.time()-start:0.2f}s")
    finally:
        pass
