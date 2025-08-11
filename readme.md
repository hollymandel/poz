Poz is a causal profiler for asyncio. It is inspired by <a href = "https://github.com/plasma-umass/coz">Coz</a>. 

If you have many functions running concurrently, it is hard to determine which are contributing most to runtime. The idea behind Coz (and Poz) is to empirically determine the impact of speeding up a given function by implementing a virtual speedup--which is actually a pause of all other concurrent functions. If the pause has size delta, the effect of the virtual speedup per call is given by `delta` minus the change in runtime due to pause. The larger the effect, the more worthwhile to optimize the target function.

Poz assumes that the target function (the function that experiences a virtual speedup) and its concurrent functions mainly interact in ways mediated by `asyncio`. This means they may compete for scheduling priority, locks or semaphores, and other asyncio resources, but do not share state in other ways.


### Examples

The following examples demonstrate capabilities and limitations of Poz. See `/test` for implementations of these examples. 
1. **The Non-Rate Limiting Step**: `F` and `G` are IO-bound functions. `F` runs several times in sequence, and each time it calls `G` once before it returns. If a pause is placed in `G` before `F` is allowed to return, then Poz will delay the return of each `F`, so the program will slow down by `delta` times the number of iterations. If the pause is placed in `F` after `G` is allowed to return, the PozLoop will force `F` to take an async pause of size `delta` before its next return, so the effect is the same. This accurately reflects the fact that optimizing `G` will not improve the runtime of the program, since it is constrained by the serial runtime of `F`.
2. **The Rate Limiting Step**: On the other hand, if a virtual speedup is in placed in `F`, runtime will be unaffected, accurately reflecting the fact that the serial runtime of `F` is limiting runtime. 
3. **The CPU Bound Workload**: `F` and `G` are CPU-bound functions that trade off access to a single thread. A virtual speedup placed in either function will not affect the runtime of the program, but only give the target function exclusive access to the thread during the pause period. This accurately reflects that fact that optimizing the runtime of either function will increase the runtime.
4. **The rate-limited external API**: The target function `F` and a concurrent function `G` compete to call an external API that will only process one request at a time. Say `F` is given a virtual speedup while `G` holds the resource for additional time. This is misleading, because `Poz` cannot prevent `G` from releasing external resources. However, speeding `F` at this point will not make `G` return any faster. Therefore the runtime of the program will not increase, incorrectly suggesting that optimizing `F` at this point will improve performance. 
5. **Asyncio Lock**: The target function `F` and a concurrent function `G` compete to acquire an asyncio lock. Say `F` is given a virtual speedup while `G` holds the lock. Poz will impose a delay on `G` before it is able to release the lock. Therefore the runtime of the program will increase, correctly reflecting that optimizing `F` at this point will not improve performance.

### Implementation
   
The pause/virtual speedup is implemented as follows. An event loop subclass, `PozLoop`, is created. A virtual speedup is placed within the target function: `PozLoop.virtual_speedup(delta)`. Then, the `PozLoop` is used in place of the default event loop to run the tasks (e.g. `pozloop.run_until_complete()`). 

When the target function is called inside a PozLoop, other functions are delayed by `delta`. This means that
1. Any work already in the `_ready` queue is rescheduled with a delay of `delta`, unless it originates from the target function.
2. Any function running concurrently when the virtual speedup is encountered will suffer a `delta` delay the next time it tries to execute any callback, i.e. the callback will be rescheduled with a delay of `delta`.
2. For a period `delta` starting from when the function is called, no new asynchronous tasks can be launched. 

Thus concurrent functions experience a pause equivalent to an `asyncio.sleep(delta)` the next time they try to affect the event loop. This may come after `delta` time has passed from the virtual speedup, which is why Poz is limited in scope to event loop-mediated interactions.  

TODO: 
- profiler/visibility
- enforce only one speedup point per script
- build out all examples, fake service for external API