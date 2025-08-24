import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop
import pickle
import pdb
import time

async def process_a(i):
    print(f"A{i} starting CPU work")

    start_time = time.time()

    # Approx 3s of CPU work
    for _ in range(12):
        sum([x**2 for x in range(1_000_000)])
        await asyncio.sleep(0)

    print(f"A{i} done, elapsed time {time.time()-start_time:0.2f} ")

        
async def process_b(i):
    print(f"B{i} starting CPU work")

    start_time = time.time()

    await PozLoop.virtual_speedup(3) # comment/uncomment - runtime of ~6s unaffected
    for _ in range(12):
        sum([x**2 for x in range(1_000_000)])
        await asyncio.sleep(0)

    print(f"B{i} done, elapsed time {time.time()-start_time:0.2f} ")


async def main():
    task1 = process_a(0)
    task2 = process_b(0)
    await asyncio.gather(task1, task2)

if __name__ == "__main__":
    loop = PozLoop()
    try:
        start_time = time.time()
        loop.run_until_complete(main())
        print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")
    finally:
        loop.close()
