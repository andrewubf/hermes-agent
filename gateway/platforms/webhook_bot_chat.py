"""Handoff of an admitted webhook's committed final to its own Bot Chat."""
import asyncio
import hashlib
import json
from pathlib import Path

from gateway.platforms.base import SendResult
from gateway.response_filters import is_autonomous_silence_response


def result_id(home, admission_id):
    identity = ['webhook-final-v1', str(Path(home).resolve()), admission_id]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _scrub(text):
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception:  # health: allow BLE001 -- redactor failures must not expose the original secret
        return '[REDACTED - redaction failed]'


def _handoff(home, admission_id, route, content):
    from tools.bot_live_delivery import read_delivery_result, find_canonical_live_owner, deliver_to_live_owner
    home = Path(home).resolve()
    key = result_id(home, admission_id)
    receipt = read_delivery_result(home, key)
    if receipt is None:
        message = (f'[Webhook automation "{_scrub(route)}" output: not the user. '
                   f'Review it, act on anything that needs action, and summarize for the chat.]\n\n{_scrub(content)}')
        owner = find_canonical_live_owner(home)
        if owner is None:
            raise ValueError('canonical Bot Chat owner unavailable')
        receipt = deliver_to_live_owner(home, owner, message, delivery_id=key)
    status = receipt['status']
    return SendResult(success=status in ('queued', 'claimed', 'settled'), message_id=key,
                      error=None if status in ('queued', 'claimed', 'settled') else str(receipt.get('error') or status),
                      raw_response={**receipt, 'delivery_id': key, 'source_admission_id': admission_id})


async def deliver_result(home, admission_id, route, content):
    if is_autonomous_silence_response(content):
        return SendResult(success=True, raw_response={'status': 'suppressed'})
    try:
        return await asyncio.to_thread(_handoff, home, admission_id, route, content)
    except Exception:  # health: allow BLE001 -- transport errors may contain the private message
        return SendResult(success=False, error='Bot Chat handoff unverified')


def _source_result(authority, adapter, event, admission_id):
    from gateway.platforms.webhook_delivery import retained_destination
    from gateway.session_results import admission_result
    from hermes_state_runtime import get_session_admission
    delivery = retained_destination(adapter, event.source.chat_id)
    if delivery['deliver'] != 'bot-chat':
        return None
    row = get_session_admission(authority.db, admission_id=admission_id)
    native = (row or {}).get('payload', {}).get('native_text_v1', {})
    if (not row or row.get('status') != 'terminal' or row.get('outcome') != 'completed'
            or native.get('automation') is not None
            or native.get('webhook_delivery') != delivery
            or native.get('source', {}).get('chat_id') != event.source.chat_id):
        raise ValueError('not the original completed webhook admission')
    saved = admission_result(authority.db, admission_id)
    result = (saved or {}).get('result', {})
    text = result.get('final_response')
    if not isinstance(text, str) or result.get('failed') or result.get('interrupted'):
        raise ValueError('committed webhook result unavailable')
    return Path(authority.db.db_path).resolve().parent, delivery['route'], text


async def deliver_settled(authority, adapter, event, admission_id):
    """Return None for another destination, else the explicit Bot Chat send result."""
    try:
        source = await asyncio.to_thread(_source_result, authority, adapter, event, admission_id)
        if source is None:
            return None
        home, route, text = source
        if is_autonomous_silence_response(text):
            return SendResult(success=True, raw_response={'status': 'suppressed'})
        media, remainder = adapter.extract_media(text)
        images, remainder = adapter.extract_images(remainder)
        files, _ = adapter.extract_local_files(remainder)
        if media or images or files or 'MEDIA:' in text or '[[as_document]]' in text:
            return SendResult(success=False, error='Bot Chat webhook delivery supports text-only finals')
        return await deliver_result(home, admission_id, route, text)
    except Exception:  # health: allow BLE001 -- refusal must not leak result data in logs
        return SendResult(success=False, error='Webhook final unavailable or unauthorized')
