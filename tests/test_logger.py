import importlib
import logging

import pytest

import grok_search.logger as logger_module
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


def test_logger_falls_back_to_null_handler_when_file_handler_fails(monkeypatch):
    base_logger = logging.getLogger("grok_search")
    original_handlers = list(base_logger.handlers)
    for handler in original_handlers:
        base_logger.removeHandler(handler)

    def fail_file_handler(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(logging, "FileHandler", fail_file_handler)

    try:
        reloaded = importlib.reload(logger_module)

        assert any(isinstance(handler, logging.NullHandler) for handler in reloaded.logger.handlers)
    finally:
        for handler in list(base_logger.handlers):
            base_logger.removeHandler(handler)
        for handler in original_handlers:
            base_logger.addHandler(handler)


def test_logger_tests_leave_global_handlers_clean():
    base_logger = logging.getLogger("grok_search")

    assert not any(isinstance(handler, logging.NullHandler) for handler in base_logger.handlers)
