# poz_logger.py
import asyncio
import contextvars
import functools

# -------- context + registry --------
CURRENT_OWNER_TID = contextvars.ContextVar("poz_owner_tid", default=None)

def _callable_name(cb):
    while isinstance(cb, functools.partial):
        cb = cb.func
    if hasattr(cb, "__self__") and hasattr(cb, "__func__"):
        return f"{cb.__func__.__module__}.{type(cb.__self__).__name__}.{cb.__func__.__name__}"
    mod = getattr(cb, "__module__", None)
    name = getattr(cb, "__qualname__", getattr(cb, "__name__", repr(cb)))
    return f"{mod}.{name}" if mod else str(name)

# -------- describers (pickle-safe) --------
def describe_task(t: asyncio.Task):
    try:
        coro = t.get_coro()
        try:
            src = _callable_name(coro.cr_code)
        except Exception:
            src = _callable_name(getattr(coro, "__call__", coro))
    except Exception:
        src = "<unknown>"
    return {
        "kind": "Task",
        "id": getattr(t, "_poz_id", None),
        "name": getattr(t, "get_name", lambda: None)(),
        "source": src,
        "done": t.done(),
        "cancelled": t.cancelled(),
    }

def _describe_callback(cb, args, owner_tid, task_index):
    name = _callable_name(cb)
    owner = task_index.get(owner_tid) if owner_tid is not None else None
    owner_info = {"tid": owner_tid}
    if owner:
        owner_info.update({"task_name": owner["name"], "task_source": owner["source"]})
    # keep args lightweight
    safeargs = []
    for a in (args or ()):
        try:
            r = repr(a)
        except Exception:
            r = f"<{type(a).__name__}>"
        safeargs.append(r if len(r) <= 120 else r[:117] + "...")
    return {
        "source": name,
        "idea": f"Callback {name}",
        "owner": owner_info,
        "args": safeargs,
    }

def describe_handle(h, task_index):
    cb = getattr(h, "_callback", None)
    args = getattr(h, "_args", ())
    owner_tid = getattr(h, "_poz_owner_tid", None) or getattr(cb, "_poz_owner_tid", None)
    base = _describe_callback(cb, args, owner_tid, task_index)
    when = getattr(h, "_when", None)
    kind = type(h).__name__
    base.update({
        "kind": kind,
        "when": when,
        "cancelled": bool(getattr(h, "_cancelled", False)),
    })
    base["idea"] = (f"{kind} scheduled at {when} for {base['source']}"
                    if when is not None else f"{kind} ready to run {base['source']}")
    return base

def describe_ready_deque(dq, task_index, limit=None):
    items = list(dq)
    if limit is not None:
        items = items[:limit]
    out = []
    for h in items:
        try:
            out.append(describe_handle(h, task_index))
        except Exception as e:
            out.append({"kind": type(h).__name__, "idea": "un-describable handle", "error": repr(e)})
    if limit is not None and len(dq) > limit:
        out.append({"kind": "TruncationNote", "idea": f"Truncated {len(dq)-limit} ready item(s)"})
    return out

def describe_scheduled_heap(heap_list, task_index, limit=None):
    items = list(heap_list)
    if limit is not None:
        items = items[:limit]
    out = []
    for h in items:
        try:
            out.append(describe_handle(h, task_index))
        except Exception as e:
            out.append({"kind": type(h).__name__, "idea": "un-describable timer", "error": repr(e)})
    if limit is not None and len(heap_list) > limit:
        out.append({"kind": "TruncationNote", "idea": f"Truncated {len(heap_list)-limit} scheduled item(s)"})
    return out

def describe_event_list(events, limit=None):
    items = list(events)
    if limit is not None:
        items = items[:limit]
    out = []
    for ev in items:
        try:
            key, mask = ev
            fd = None
            try:
                if hasattr(key, "fd") and key.fd is not None:
                    fd = key.fd
                elif hasattr(key, "fileobj"):
                    fo = key.fileobj
                    if isinstance(fo, int):
                        fd = fo
                    elif hasattr(fo, "fileno"):
                        try:
                            fd = fo.fileno()
                        except Exception:
                            fd = None
            except Exception:
                fd = None
            data_type = type(getattr(key, "data", None)).__name__
            out.append({
                "kind": "SelectorEvent",
                "fd": fd,
                "mask": int(mask) if isinstance(mask, int) else None,
                "key_data_type": data_type,
                "idea": f"I/O event on fd={fd} mask={mask}",
            })
        except Exception as e:
            out.append({"kind": "SelectorEvent", "idea": "un-describable event", "error": repr(e)})
    if limit is not None and len(events) > limit:
        out.append({"kind": "TruncationNote", "idea": f"Truncated {len(events)-limit} event(s)"})
    return out

def describe_task_list(tasks):
    out = []
    for t in tasks:
        try:
            out.append(describe_task(t))
        except Exception as e:
            out.append({"kind": "Task", "idea": "un-describable task", "error": repr(e)})
    return out

# -------- mixin that tags owners + manages registry --------
class PozOwnerMixin:
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._poz_tid_counter = 0
        self._poz_task_index = {}  # tid -> {"id","name","source"}

    async def _poz_task_entry(self, tid, coro):
        tok = CURRENT_OWNER_TID.set(tid)
        try:
            return await coro
        finally:
            CURRENT_OWNER_TID.reset(tok)

    def _poz_alloc_tid(self):
        self._poz_tid_counter += 1
        return self._poz_tid_counter

    def _poz_record_task_meta(self, task, coro, tid):
        task._poz_id = tid
        try:
            name = task.get_name()
        except Exception:
            name = f"Task-{tid}"
        try:
            src = _callable_name(coro.cr_code)
        except Exception:
            try:
                src = _callable_name(getattr(coro, "__call__", coro))
            except Exception:
                src = "<unknown>"
        self._poz_task_index[tid] = {"id": tid, "name": name, "source": str(src)}

    def create_task(self, coro, *, name=None, context=None):
        tid = self._poz_alloc_tid()

        # Owner of the newly created task (who spawned it)
        creator_tid = CURRENT_OWNER_TID.get()
        # Inside the new task, we want CURRENT_OWNER_TID == tid
        wrapped = self._poz_task_entry(tid, coro)

        # Important: creation happens in the creator's context (so handles made
        # during immediate scheduling will be attributed to the creator). Once
        # the task starts running, it will switch to its own tid via wrapped.
        t = super().create_task(wrapped, name=name, context=context)
        self._poz_record_task_meta(t, coro, tid)

        # Also tag the task object with its *creator* for reference (optional)
        try:
            t._poz_creator_tid = creator_tid
        except Exception:
            pass
        return t

    # Tagging handle creators
    def call_soon(self, callback, *args, context=None):
        h = super().call_soon(callback, *args, context=context)
        setattr(h, "_poz_owner_tid", CURRENT_OWNER_TID.get())
        return h

    def call_at(self, when, callback, *args, context=None):
        h = super().call_at(when, callback, *args, context=context)
        setattr(h, "_poz_owner_tid", CURRENT_OWNER_TID.get())
        return h

    def call_later(self, delay, callback, *args, context=None):
        h = super().call_later(delay, callback, *args, context=context)
        setattr(h, "_poz_owner_tid", CURRENT_OWNER_TID.get())
        return h

    # add_reader/add_writer often return None; tag callback itself
    def add_reader(self, fd, callback, *args):
        h = super().add_reader(fd, callback, *args)
        try:
            callback._poz_owner_tid = CURRENT_OWNER_TID.get()
        except Exception:
            pass
        return h

    def add_writer(self, fd, callback, *args):
        h = super().add_writer(fd, callback, *args)
        try:
            callback._poz_owner_tid = CURRENT_OWNER_TID.get()
        except Exception:
            pass
        return h

__all__ = [
    "CURRENT_OWNER_TID",
    "PozOwnerMixin",
    "describe_task",
    "describe_task_list",
    "describe_handle",
    "describe_ready_deque",
    "describe_scheduled_heap",
    "describe_event_list",
]
