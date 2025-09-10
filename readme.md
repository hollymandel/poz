Poz is a causal profiler for asyncio. It is inspired by <a href = "https://github.com/plasma-umass/coz">Coz</a>. 

If you have many functions running concurrently, it is hard to determine which are contributing most to runtime. The idea behind Coz (and Poz) is to empirically determine the impact of speeding up a given function by implementing a virtual speedup--which is actually a pause of all other concurrent functions. If the pause has size delta, the effect of the virtual speedup per call is given by `delta` minus the change in runtime due to pause. The larger the effect, the more worthwhile to optimize the target function.

Poz assumes that the target function (the function that experiences a virtual speedup) and its concurrent functions mainly interact in ways mediated by `asyncio`. This means they may compete for scheduling priority, locks or semaphores, and other asyncio resources, but do not share state in other ways.


### Examples

The following examples demonstrate capabilities and limitations of Poz. In all cases except 5, Poz gives accurate results. See `/examples` for implementations.
1. **The Non-Rate Limiting Step** (`/examples/rate_limiting_example.py`): `F` and `G` are IO-bound functions. `F` runs several times in sequence, and each time it calls `G` once before it returns. If a pause is placed in `G` before `F` is allowed to return, then Poz will delay the return of each `F`, so the program will slow down by `delta` times the number of iterations. If the pause is placed in `F` after `G` is allowed to return, the PozLoop will force `F` to take an async pause of size `delta` before its next return, so the effect is the same. This accurately reflects the fact that optimizing `G` will not improve the runtime of the program, since it is constrained by the serial runtime of `F`.
2. **The Rate Limiting Step** (`/examples/rate_limiting_example.py`): On the other hand, if a virtual speedup is in placed in `F`, runtime will be unaffected, accurately reflecting the fact that the serial runtime of `F` is limiting runtime. 
3. **The Surprising Slowdown** (`/examples/paradoxical_slowdown.py`): `F` and `G` each run three times in series (ie FFF and GGG, concurrently) and compete for a lock. The program begins by launching `F` and then immediately launches `G`. `F` holds the lock of 1s and then runs for 5s without the lock. `F` runs for 3s without the lock and then runs with the lock for 3s. This timing is well-coordinated, so the total runtime is 18s. But say you optimize the first 
4. **The CPU Bound Workload**  (`/examples/CPU_bound.py`): `F` and `G` are CPU-bound functions that trade off access to a single thread. A virtual speedup placed in either function will not affect the runtime of the program, but only give the target function exclusive access to the thread during the pause period. This accurately reflects that fact that optimizing the runtime of either function will increase the runtime.
5. **The Rate-Limited External API**: The target function `F` and a concurrent function `G` compete to call an external API that will only process one request at a time. Say `F` is given a virtual speedup while `G` holds the resource for additional time. This is misleading, because `Poz` cannot prevent `G` from releasing external resources. However, speeding `F` at this point will not make `G` return any faster. Therefore the runtime of the program will not increase, incorrectly suggesting that optimizing `F` at this point will improve performance. 
6. **The Asyncio Lock** (`/examples/lock_competition.py`): The target function `F` and a concurrent function `G` compete to acquire an asyncio lock. Say `F` is given a virtual speedup while `G` holds the lock. Poz will impose a delay on `G` before it is able to release the lock. Therefore the runtime of the program will increase, correctly reflecting that optimizing `F` at this point will not improve performance. On the other hand, if `F` is given a virtual speedup while it holds the lock, the program runtime will not increase, reflecting the fact that optimizing `F` will improve runtime. SPECIAL LOGIC NEEDED HERE IF TARGET FUNCTION HOLDING A LOCK? PAUSE EVENT LOOP?
7. **The Suspended Thread** (`/examples/suspended_thread.py`): `F`. `G`, and `H` are coroutines. `G` and `H` compete for an asyncio lock while `F` does something unrelated. While `G` is holding the lock and `H` is waiting for it, `F` runs with a virtual speedup. Then `G` receives a delay of `delta` when it tries to return the lock, and `H` receives a delay of delta when it tries to acquire it. But since these delays are concurrent, there is no double counting. (IS THIS RIGHT? WHAT DOES IT MEAN TO TRY TO ACQUIRE THE LOCK? I'm guessing every time you run once that waiting function just checks to see if it can get the lock. So this callback will just be scheduled at a delay of delta. Is there a risk that some slow thing will occur between the callback reschedule of the release and the callback reschedule of the acquire? Depend son how these things are ordered. One hopes not.)

### Implementation
   
The pause/virtual speedup is implemented as follows. An event loop subclass, `PozLoop`, is instantiated. A virtual speedup is placed within the target function: `PozLoop.virtual_speedup(delta)`. Then, the `PozLoop` is used in place of the default event loop to run the tasks. To do this, you can either call `asyncio.run` from within `PozPolicy()` context, or just call `pozloop.run_until_complete()`:

```code example block here```

When the target function is called inside a PozLoop, other functions are delayed by `delta`. This means that
1. Any work already in the `_ready` queue is rescheduled with a delay of `delta`, unless it originates from the target function.
2. Any function running concurrently when the virtual speedup is encountered will suffer a `delta` delay the next time it tries to execute any callback, i.e. the callback will be rescheduled with a delay of `delta`.
3. For a period `delta` starting from when the function is called, no new asynchronous tasks can be launched. 

Thus concurrent functions experience a pause equivalent to an `asyncio.sleep(delta)` the next time they try to affect the event loop. This may come after `delta` time has passed from the virtual speedup, which is why Poz is limited in scope to event loop-mediated interactions.  

Does Poz deal appropriately with suspended coroutine? Yes--because in asyncio, coroutines that are waiting on other coroutines have a callback in every pass through the event loop to check for readiness, which then spawns a new check-if-ready callback. If a thread is paused, this callback will be rescheduled (once) with an async delay of `delta`. If the thread is unpaused after this delay, the delay will be "silent", which is the desired behavior. 



### Limitations

Poz assumes that the important interactions between coroutines are mediated by the event loop. See "the rate-limited external API" for an example failure case. 

Poz does not control for the overhead of rescheduling tasks, etc. This overhead is on the order of ????. Therefore Poz cannot reliably detect effects on this time scale.

Python program runtimes are noisy. (Is this true rel other languages?) Multiple experiments may be needed to resolve small effects, and results may be influenced by other processes (is this true? GIL? multi thread CPU?)



TODO: 
- profiler/visibility
- build out all examples, fake service for external API
- DIFFERENT FORMULA? 1-po/ps?
- SUSPENDED THREADS? Thread B holds a lock and then releases, thread C acquires the lock, should get credit for B's delay. But I think it might just work, because when B gets a delay to reeturn the lock, C gets an async delay to acquire it, and they overlap. I guess it depends on how
- PENALIZE_ONCE.discard issue!! 
- thread safety (call_soon_threadsafe)
- cancelled task handling?
- keep track of parentage and remove delay penalty from parents (but not siblings)

Traceback (most recent call last):
  File "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/asyncio/events.py", line 80, in _run
    self._context.run(self._callback, *self._args)
  File "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/asyncio/base_events.py", line 750, in call_soon
    handle = self._call_soon(callback, args, context)
  File "/Users/hollymandel/poz/poz_proto.py", line 244, in _call_soon_shim
    PENALIZE_ONCE.discard(task)
AttributeError: 'WeakKeyDictionary' object has no attribute 'discard'
G released lock