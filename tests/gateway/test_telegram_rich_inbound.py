"""Regression tests for inbound Telegram rich messages (Bot API rich format).

Telegram delivers styled/multi-block content (paragraphs, tables, photos) ONLY in
``message.rich_message``; the legacy ``text``/``photo`` fields stay empty. PTB 22.8 has no
``Message.rich_message`` attribute, so ``Update.de_json`` drops the payload — with the stock
``filters.TEXT``-based handler table such updates match nothing and are silently discarded:
no log line, no error, no agent wake (upstream #146067 / #63485).

The gateway test-suite runs against a mocked ``telegram`` package (see
``tests/gateway/conftest.py``), so these tests exercise the adapter's own logic at the
seams that do not depend on PTB objects:

- the sidecar stash at the getUpdates observation seam, including for fenced-generation
  responses (rebuild windows are exactly where updates are lost);
- the rich handler: block rendering (paragraphs, pipe-row tables, photo placeholders),
  photo-block download, exactly one delivered MessageEvent, sidecar consumption.

The PTB-level fact (``Update.de_json`` loses ``rich_message``) and the handler-table
filter match are verified against the real library in a live environment; a synthetic
repro is in the linked issue.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter

# Anonymized shape of a real production payload: two paragraphs, a bordered table with a
# bold caption, and a photo block (sizes ascending). Legacy text/photo fields are absent.
RICH_BLOCKS = {
    "blocks": [
        {"type": "paragraph", "text": "First paragraph"},
        {"type": "paragraph", "text": "Second paragraph"},
        {
            "type": "table",
            "cells": [
                [{"text": "H1"}, {"text": "H2"}],
                [{"text": "a1"}, {"text": "a2"}],
            ],
            "caption": {"type": "bold", "text": "Table caption"},
            "is_bordered": True,
        },
        {
            "type": "photo",
            "photo": [
                {"file_id": "AgADsmall", "width": 90, "height": 46},
                {"file_id": "AgADlarge", "width": 924, "height": 475},
            ],
        },
    ]
}

RICH_RAW_UPDATE = {
    "update_id": 295386729,
    "message": {
        "message_id": 53316,
        "from": {"id": 123456, "is_bot": False, "first_name": "Test"},
        "chat": {"id": 123456, "first_name": "Test", "type": "private"},
        "date": 1794111663,
        "message_thread_id": 4242,
        "is_topic_message": True,
        "rich_message": RICH_BLOCKS,
    },
}


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={}))


def _rich_message():
    chat = SimpleNamespace(id=123456, type="private", title=None)
    user = SimpleNamespace(id=123456, is_bot=False, first_name="Test")
    return SimpleNamespace(
        message_id=53316, chat_id=123456, chat=chat, from_user=user,
        text=None, caption=None, photo=None, document=None, video=None,
        audio=None, voice=None, sticker=None, location=None, poll=None)


def _rich_update():
    return SimpleNamespace(update_id=295386729, message=_rich_message())


# ---------------------------------------------------------------------------
# 1. Wire seam stashes the payload — even for a fenced (superseded) generation
# ---------------------------------------------------------------------------

def _observe(adapter: TelegramAdapter, raw_updates, generation):
    request = MagicMock()
    request.parse_json_payload.return_value = {"ok": True, "result": raw_updates}
    adapter._observe_polling_request_result(request, generation, (200, b"{}"))


def test_wire_seam_stashes_rich_sidecar():
    adapter = _make_adapter()
    adapter._record_polling_progress = MagicMock(return_value=False)  # fenced generation
    _observe(adapter, [RICH_RAW_UPDATE], generation=7)
    assert adapter._rich_message_sidecar == {"123456:53316": RICH_BLOCKS}


def test_wire_seam_ignores_plain_updates():
    adapter = _make_adapter()
    adapter._record_polling_progress = MagicMock(return_value=True)
    adapter._record_updates_received = MagicMock()
    plain = {"update_id": 2, "message": {
        "message_id": 9, "date": 1, "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False}, "text": "hello"}}
    _observe(adapter, [plain], generation=1)
    assert adapter._rich_message_sidecar == {}


def test_wire_seam_ignores_malformed_entries():
    adapter = _make_adapter()
    adapter._record_polling_progress = MagicMock(return_value=True)
    adapter._record_updates_received = MagicMock()
    _observe(adapter, [None, {"update_id": 5}, {"update_id": 6, "message": None}], generation=1)
    assert adapter._rich_message_sidecar == {}


# ---------------------------------------------------------------------------
# 2. Handler: renders blocks, downloads photos, delivers one event
# ---------------------------------------------------------------------------

def _handler_adapter(monkeypatch, delivered):
    adapter = _make_adapter()
    adapter._is_user_authorized_from_message = lambda message: True
    adapter._should_process_message = lambda message, **kw: True

    def _fake_build(message, msg_type, update_id=None):
        return MessageEvent(
            text="", message_type=msg_type,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="123456", chat_type="dm"))

    monkeypatch.setattr(adapter, "_build_message_event", _fake_build)
    monkeypatch.setattr(adapter, "_apply_telegram_group_observe_attribution", lambda event: event)
    adapter.handle_message = AsyncMock(side_effect=lambda event: delivered.append(event))

    async def _fake_cache(data, ext):
        return "/tmp/fake-rich.jpg"

    monkeypatch.setattr(
        sys.modules[TelegramAdapter.__module__], "cache_image_from_bytes_async", _fake_cache)
    return adapter


@pytest.mark.asyncio
async def test_rich_handler_delivers_rendered_event(monkeypatch):
    delivered = []
    adapter = _handler_adapter(monkeypatch, delivered)
    file_obj = MagicMock()
    file_obj.file_path = None
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"jpg-bytes"))
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=file_obj)))

    adapter._rich_message_sidecar["123456:53316"] = RICH_BLOCKS
    await adapter._handle_rich_message(_rich_update(), context)

    assert len(delivered) == 1
    text = delivered[0].text
    assert "First paragraph" in text
    assert "Second paragraph" in text
    assert "| H1 | H2 |" in text
    assert "| a1 | a2 |" in text
    assert "**Table caption**" in text
    assert "[изображение]" in text
    # Largest photo size downloaded and attached exactly once.
    context.bot.get_file.assert_awaited_once_with("AgADlarge")
    assert delivered[0].media_urls == ["/tmp/fake-rich.jpg"]
    assert delivered[0].message_type == MessageType.PHOTO
    # Sidecar entry consumed.
    assert adapter._rich_message_sidecar == {}


@pytest.mark.asyncio
async def test_rich_handler_without_sidecar_is_noop(monkeypatch):
    delivered = []
    adapter = _handler_adapter(monkeypatch, delivered)
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))
    await adapter._handle_rich_message(_rich_update(), context)
    assert delivered == []
    context.bot.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_handler_blocks_unauthorized(monkeypatch):
    delivered = []
    adapter = _handler_adapter(monkeypatch, delivered)
    adapter._is_user_authorized_from_message = lambda message: False
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))
    adapter._rich_message_sidecar["123456:53316"] = RICH_BLOCKS
    await adapter._handle_rich_message(_rich_update(), context)
    assert delivered == []
