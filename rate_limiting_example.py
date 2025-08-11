import asyncio
from poz_proto import PozLoop  # assuming PozLoop is your custom event loop
import pickle
import pdb

# Simulate longer-running process A
async def process_a(i):
    print(f"A{i} start")
    await asyncio.sleep(2)  
    PozLoop.virtual_speedup(1)
    print(f"A{i} done — kicking off B{i}")
    asyncio.create_task(process_b(i))  # trigger B after A completes
    await asyncio.sleep(0.01) # take some time to return - time for process B virtual speedup to intercede

# Simulate shorter process B
async def process_b(i):
    print(f"B{i} start")
    # PozLoop.virtual_speedup(1)
    await asyncio.sleep(1)
    print(f"B{i} done")

async def main():
    for i in range(3): 
        await process_a(i)

if __name__ == "__main__":
    loop = PozLoop(record=False)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        pass
