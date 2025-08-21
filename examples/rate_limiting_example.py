import asyncio
import sys
sys.path.append("/Users/hollymandel/poz")
from poz_proto import PozLoop
import pickle
import pdb
import time

# Simulate longer-running process A
async def process_a(i):
    print(f"A{i} start")

    PozLoop.virtual_speedup(0)
    await asyncio.sleep(2)  
    print(f"A{i} done — kicking off B{i}")

    await process_b(0)
    # asyncio.create_task(process_b(i))  # trigger B after A completes
    # await asyncio.sleep(0.01) # take some time to return - time for process B virtual speedup to intercede

# Simulate shorter process B
async def process_b(i):
    await asyncio.sleep(0)
    print(f"B{i} start")
    # PozLoop.virtual_speedup(1)
    await asyncio.sleep(1)
    print(f"B{i} done")

async def main():
    await process_a(0)

if __name__ == "__main__":
    loop = PozLoop()
    # asyncio.set_event_loop(loop)
    try:
        start_time = time.time()
        loop.run_until_complete(main())
        print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")
    finally:
        loop.close()
