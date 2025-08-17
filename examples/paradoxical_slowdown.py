import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop 
import time

async def process_a(i, lock, start_time):
    if i == 2:
        return

    await asyncio.sleep(0)
    async with lock:
        print(f"[dt = {time.time()-start_time:0.2f}] A{i} has the lock")
        await asyncio.sleep(1)
    print(f"[dt = {time.time()-start_time:0.2f}] A{i} released the lock")
    await asyncio.sleep(5)
    
    await process_a(i+1, lock, start_time)
    
async def process_b(i, lock, start_time):
    if i == 2:
        return

    await asyncio.sleep(0)

    ## Uncomment for virtual speedup. Should change task order from 
    ## ABAB to ABBA, increasing total runtime. 
    ## Expected runtime without pause: 12s
    ## Expection runtime with pause: 19s (+6s for poz delays, +1s for slowdown)
    print(f"[dt = {time.time()-start_time:0.2f}] B{i} speedup, A will be delayed in releasing/acquiring lock for 3s")
    PozLoop.virtual_speedup(3)

    await asyncio.sleep(3)

    async with lock:
        print(f"[dt = {time.time()-start_time:0.2f}] B{i} has the lock")
        await asyncio.sleep(3)
    print(f"[dt = {time.time()-start_time:0.2f}] B{i} released the lock")

    await process_b(i+1, lock, start_time)

def main():
    loop = PozLoop()
    asyncio.set_event_loop(loop)
    lock = asyncio.Lock()
    start_time = time.time()

    async def _runner():
        a0 = asyncio.create_task(process_a(0,lock, start_time))
        b0 = asyncio.create_task(process_b(0,lock, start_time))
        await asyncio.gather(a0,b0)

    loop.run_until_complete(_runner())
    loop.close()

if __name__ == "__main__":
    try:
        main()
    finally:
        pass
