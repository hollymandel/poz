import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop, PozPolicy
import time 

async def F(lock):
    await asyncio.sleep(0)
    # PozLoop.virtual_speedup(2)
    for i in range(1):
        async with lock:
            await PozLoop.virtual_speedup(1) 
            print(f"F has the lock, iteration {i}")
            start = time.time()
            sum([x**2 for x in range(10_000_000)])
            print(f"{time.time()-start:0.2f}")
            await asyncio.sleep(0)

async def G(lock):
    await asyncio.sleep(0)
    print("G launched")
    for i in range(1):
        # await PozLoop.virtual_speedup(1) 
        async with lock:
            print(f"G has the lock, iteration {i}")
            sum([x**2 for x in range(10_000_000)])
            await asyncio.sleep(0)

# async def main():
def main():
    lock = asyncio.Lock()
    loop = PozLoop()
    asyncio.set_event_loop(loop)
    try:
        async def _runner():
            t1 = asyncio.create_task(F(lock))
            t2 = asyncio.create_task(G(lock))
            await asyncio.gather(t1, t2)
        loop.run_until_complete(_runner())
    finally:
        loop.close()

if __name__ == "__main__":
    start_time = time.time()

    main()
    
    print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")