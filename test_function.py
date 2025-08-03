from poz_proto import poz_target, PozLoop
import asyncio
import time

@poz_target
async def target():
    for i in range(3):
        print("target tick", i)
        await asyncio.sleep(0.05)
    print("target done")

async def other(name):
    for i in range(3):
        print(f"{name} tick", i)
        await asyncio.sleep(0.05)
    print(f"{name} done")

def main():
    loop = PozLoop()
    asyncio.set_event_loop(loop)
    try:
        async def _runner():
            t1 = asyncio.create_task(target())
            t2 = asyncio.create_task(other("A"))
            t3 = asyncio.create_task(other("B"))
            await asyncio.gather(t1, t2, t3)
        loop.run_until_complete(_runner())
    finally:
        loop.close()

if __name__ == "__main__":
    main()

