import threading
from types import SimpleNamespace

import pytest

from gateway.platforms.webhook_delivery import validate_destination, snapshot_destination
from hermes_state_runtime import RuntimeStoreError


def test_bot_chat_destination_roundtrip_and_closed_target():
    source = {'deliver': 'bot-chat', 'deliver_extra': {}, 'route': 'orders', 'profile': 'other'}
    assert snapshot_destination(None, source) == source
    for patch in ({'deliver_extra': {'chat_id': 'other'}}, {'mirror': True}, {'deliver': 'bot-chat:other'}):
        with pytest.raises(RuntimeStoreError):
            validate_destination({**source, **patch})


@pytest.mark.asyncio
async def test_native_receipt_offloop_and_result_identity(monkeypatch, tmp_path):
    from gateway.platforms.webhook_bot_chat import deliver_result
    from tools import bot_live_delivery
    calls = []
    loop_thread = threading.get_ident()
    monkeypatch.setattr(bot_live_delivery, 'read_delivery_result', lambda home, key: None)
    monkeypatch.setattr(bot_live_delivery, 'find_canonical_live_owner', lambda home: {'session_id': 'bot'})
    def deliver(home, owner, message, *, delivery_id):
        calls.append((home, delivery_id, message, threading.get_ident()))
        return {'status': 'queued', 'message': message, 'admission_id': 'target'}
    monkeypatch.setattr(bot_live_delivery, 'deliver_to_live_owner', deliver)
    a = await deliver_result(tmp_path, 'admission-a', 'orders', 'same output')
    await deliver_result(tmp_path, 'admission-a', 'orders', 'same output')
    await deliver_result(tmp_path, 'admission-b', 'orders', 'same output')
    assert a.raw_response['status'] == 'queued'
    assert a.success and a.raw_response['status'] != 'settled'
    assert calls[0][1] == calls[1][1] != calls[2][1]
    assert all(call[3] != loop_thread for call in calls)
    assert 'Webhook automation' in calls[0][2] and 'not the user' in calls[0][2]


@pytest.mark.asyncio
async def test_existing_receipt_is_observed_without_discovery(monkeypatch, tmp_path):
    from gateway.platforms.webhook_bot_chat import deliver_result
    from tools import bot_live_delivery
    receipt = {'status': 'claimed', 'message': 'previous immutable message', 'admission_id': 'target'}
    monkeypatch.setattr(bot_live_delivery, 'read_delivery_result', lambda home, key: receipt)
    def forbidden(home):
        pytest.fail('a receipt must be read before owner discovery')
    monkeypatch.setattr(bot_live_delivery, 'find_canonical_live_owner', forbidden)
    result = await deliver_result(tmp_path, 'source', 'route', 'output')
    assert result.success and result.raw_response['status'] == 'claimed'
    assert result.raw_response['message'] == 'previous immutable message'


@pytest.mark.asyncio
async def test_silence_and_force_redaction(monkeypatch, tmp_path):
    from gateway.platforms.webhook_bot_chat import deliver_result
    from agent import redact
    from tools import bot_live_delivery
    monkeypatch.setattr(redact, 'redact_sensitive_text', lambda text, force=False: '[scrubbed]' if force else text)
    seen = []
    monkeypatch.setattr(bot_live_delivery, 'read_delivery_result', lambda home, key: None)
    monkeypatch.setattr(bot_live_delivery, 'find_canonical_live_owner', lambda home: {})
    def deliver(home, owner, message, *, delivery_id):
        seen.append(message)
        return {'status': 'settled', 'message': message}
    monkeypatch.setattr(bot_live_delivery, 'deliver_to_live_owner', deliver)
    silent = await deliver_result(tmp_path, 'a', 'secret', 'NO_REPLY')
    assert silent.success and not seen
    result = await deliver_result(tmp_path, 'b', 'secret', 'secret')
    assert result.success and 'secret' not in seen[0] and seen[0].count('[scrubbed]') == 2


@pytest.mark.asyncio
async def test_settled_uses_original_committed_result_and_owner(monkeypatch, tmp_path):
    from gateway.platforms import webhook_bot_chat, webhook_delivery
    from gateway import session_results
    import hermes_state_runtime
    delivery = {'deliver': 'bot-chat', 'deliver_extra': {}, 'route': 'orders'}
    native = {'webhook_delivery': delivery, 'source': {'chat_id': 'event'}}
    row = {'status': 'terminal', 'outcome': 'completed', 'payload': {'native_text_v1': native}}
    monkeypatch.setattr(webhook_delivery, 'retained_destination', lambda adapter, chat: delivery)
    monkeypatch.setattr(hermes_state_runtime, 'get_session_admission', lambda db, admission_id: row)
    monkeypatch.setattr(session_results, 'admission_result', lambda db, admission_id: {'result': {'final_response': 'committed'}})
    sent = []
    async def send(home, admission_id, route, content):
        sent.append((home, admission_id, route, content))
        return SimpleNamespace(success=True)
    monkeypatch.setattr(webhook_bot_chat, 'deliver_result', send)
    from gateway.platforms.base import BasePlatformAdapter
    adapter = SimpleNamespace(extract_media=BasePlatformAdapter.extract_media,
                              extract_images=BasePlatformAdapter.extract_images,
                              extract_local_files=BasePlatformAdapter.extract_local_files)
    authority = SimpleNamespace(db=SimpleNamespace(db_path=tmp_path / 'state.db'))
    event = SimpleNamespace(source=SimpleNamespace(chat_id='event'))
    result = await webhook_bot_chat.deliver_settled(authority, adapter, event, 'source-id')
    assert result.success and sent == [(tmp_path, 'source-id', 'orders', 'committed')]
    native['automation'] = {'followup': True}
    assert not (await webhook_bot_chat.deliver_settled(authority, adapter, event, 'other')).success
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_media_final_is_refused_before_transport(monkeypatch, tmp_path):
    from gateway.platforms import webhook_bot_chat
    from gateway.platforms.base import BasePlatformAdapter
    monkeypatch.setattr(webhook_bot_chat, '_source_result', lambda *args: (tmp_path, 'route', 'MEDIA: /x/image.png'))
    adapter = SimpleNamespace(extract_media=BasePlatformAdapter.extract_media,
                              extract_images=BasePlatformAdapter.extract_images,
                              extract_local_files=BasePlatformAdapter.extract_local_files)
    result = await webhook_bot_chat.deliver_settled(None, adapter, None, 'source')
    assert not result.success and 'text-only' in result.error
