import pytest

from grok_search.logger import log_info


class DummyContext:
    def __init__(self):
        self.messages = []

    async def info(self, message: str):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_log_info_skips_ctx_when_debug_disabled():
    ctx = DummyContext()

    await log_info(ctx, "sensitive message", is_debug=False)

    assert ctx.messages == []


@pytest.mark.asyncio
async def test_log_info_forwards_ctx_when_debug_enabled():
    ctx = DummyContext()

    await log_info(ctx, "debug message", is_debug=True)

    assert ctx.messages == ["debug message"]
