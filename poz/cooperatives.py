import asyncio
import inspect
from contextvars import ContextVar

_poz_cooperative = ContextVar("_poz_cooperative", default=False)

if hasattr(asyncio, "TaskGroup"):
    class CooperativeTaskGroup:
        def __init__(self):
            self._tg = asyncio.TaskGroup()

        async def __aenter__(self):
            return await self._tg.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            # Ensure the cooperative flag is set before creating/awaiting
            # the underlying TaskGroup's __aexit__ awaitable.
            tok = _poz_cooperative.set(True)
            try:
                return await self._tg.__aexit__(exc_type, exc, tb)
            finally:
                _poz_cooperative.reset(tok)

        def create_task(self, *args, **kwargs):
            return self._tg.create_task(*args, **kwargs)
