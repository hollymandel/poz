import asyncio
from contextvars import ContextVar

_poz_cooperative = ContextVar("_poz_cooperative", default=False)

async def _cooperative_await(awaitable):
    tok1 = _poz_cooperative.set(True)
    try:
        return await awaitable
    finally:
        _poz_cooperative.reset(tok1)