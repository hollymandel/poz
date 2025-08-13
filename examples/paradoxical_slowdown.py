import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop 

async def process_a(i, lock):
    if i == 2:
        return

    await asyncio.sleep(0)
    async with lock:
        print(f"A{i} has the lock")
        await asyncio.sleep(1)
    print(f"A{i} released the lock")
    await asyncio.sleep(5)
    
    await process_a(i+1, lock)
    
async def process_b(i, lock):
    if i == 2:
        return

    # await asyncio.sleep(3) # original - expected total runtime 12s
    await asyncio.sleep(0) # simulated optimization, expected total runtime 13s

    async with lock:
        print(f"B{i} has the lock")
        await asyncio.sleep(3)
    print(f"B{i} released the lock")

    await process_b(i+1, lock)

def main():
    loop = PozLoop()
    asyncio.set_event_loop(loop)
    lock = asyncio.Lock()

    async def _runner():
        a0 = asyncio.create_task(process_a(0,lock))
        b0 = asyncio.create_task(process_b(0,lock))
        await asyncio.gather(a0,b0)

    loop.run_until_complete(_runner())
    loop.close()

if __name__ == "__main__":
    try:
        main()
    finally:
        pass
