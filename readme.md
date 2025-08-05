Poz is a causal profiler for asyncio. It is inspired by <a href = "https://github.com/plasma-umass/coz">Coz</a>. 

If you have many functions running concurrently, it is hard to determine which are contributing most to runtime. The idea behind Coz (and Poz) is to empirically determine the impact of speeding up a given function by implementing a virtual speedup--which is actually a pause of all other concurrent functions. If the pause has size delta, the effect of the virtual speedup per call is given by `delta` minus the change in runtime due to pause. The larger the effect, the more worthwhile to optimize the target function.

Poz assumes that the target function and its concurrent functions mainly interact in ways mediated by `asyncio`. This means they may compete for scheduling priority, locks or semaphores, and other asyncio resources, but do not share state in others ways.

The following examples demonstrate capabilities and limitations of Poz.
1. **The Rate Limiting Step**: `F` and `G` are IO-bound functions. `F` runs several times in sequence, and each time it calls `G` once before it returns. If `G` is the target function, then Poz will delay the return of each `F`, so the program will slow down by `delta` times the number of iterations. This accurately reflects the fact that optimizing `G` will not improve the runtime of the program, since it is constrained by the nonparallelism of `F`.
2. **The CPU Bound Workload**: No slowdown, speedup has effect, this is accurate.
3. **The rate-limited external API**: The target function `F` and a concurrent function `G` compete to call an external API that will only process one request at a time. Say `F` is called with `delta` = 2s while `G` holds the resource for an additional 1s. This is misleading, because speeding `F` at htis point will not make `G` return any faster.
4. **Asyncio Lock**: The target function `F` and a concurrent function `G` compete to call an external API that will only process one request at a time. Say `F` is called with `delta` = 2s while `G` holds the resource for an additional 1s. This will delay the return of the lock by 2s, showing that it is not helpful to speed `F` by 2s at that time point, because it is waiting.  But if called after lock release it's good. 

You can think of the virtual speedup as occuring (1) starting at the point where called and (2) going until it hits an asyncio-affecting boundary. 
The following two examples demonstrate limitations of Poz:
1. The target function `F` and a concurrent function `G` compete to call an external API that will only process one request at a time. In this case Poz will report that the virtual speedup has no effect, because `G` will release the resource before being subject to the asyncio-imposed delay. But clearly optimizing `F` will speed the process. 
2. `F` and `G` are CPU-bound, and the event loop switches between them. If `F` is called, `G` will suffer a delay, but during the delay the event loop will pass CPU resources back to `F`, so the total runtime will not increase.  
   
The pause/virtual speedup is implemented as follows. A target function is marked with a decorator (`@poz_target`). An event loop subclass, `PozLoop`, is created, and tasks are run via this loop (e.g. `pozloop.run_until_complete()`). Then, when the target function is called inside a PozLoop, other functions are delayed by `delta`. This means that
1. For any function running asynchronously when the target function is called, it will suffer a `delta` delay the next time it tries to do any event-loop-affecting action, such as returning, moving work onto the event loop queue, or releasing/acquiring a lock.
2. For `delta` starting from when the function is called, no new asynchronous tasks can be scheduled by the cpu.

The delay is modeled as instantaneous at the moment of the target function call. 
