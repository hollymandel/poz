import asyncio
import sys
import poz
import pickle
import pdb
import time

def print_with_time(start_time, msg):
    print(f"[{time.time()-start_time:0.2f}] {msg}")
# Simulate longer-running process A
async def process_a(i, start_time):
    await asyncio.sleep(0)
    print_with_time(start_time, f"A{i} start")

    poz.virtual_speedup(.2)
    await asyncio.sleep(.2)  
    print_with_time(start_time, f"A{i} done — kicking off B{i}")

    # return asyncio.create_task(process_b(i), name=f"B{i}")
    return asyncio.create_task(process_b(i, start_time))  # trigger B after A completes
    # await asyncio.sleep(0.01) # take some time to return - time for process B virtual speedup to intercede

# Simulate shorter process B
async def process_b(i, start_time):
    await asyncio.sleep(0)
    print_with_time(start_time, f"B{i} start")
    # PozLoop.virtual_speedup(1)
    # poz.virtual_speedup(.1)
    await asyncio.sleep(.1)
    print_with_time(start_time, f"B{i} done")

async def main():
    start_time = time.time()
    # tasks = [ asyncio.create_task(process_a(i, start_time)) for i in range(3) ]
    # await asyncio.gather(*tasks)
    # # b_tasks = []
    # 
    for i in range(3):
        # new_b = await process_a(i)
        last = await process_a(i, start_time)
    await last
    #     # b_tasks.append(new_b)
    # # await asyncio.gather(*b_tasks)

if __name__ == "__main__":
    start_time = time.time()
    with poz.poz_context():
        asyncio.run(main())
        print("finished!")

    print(f"Poz Loop Elapsed time: {time.time() - start_time:0.4f} s")
