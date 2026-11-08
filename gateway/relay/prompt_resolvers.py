"""Relay prompt answers: who may answer a native prompt, and how each prompt kind resolves.

``RelayAdapter._consume_prompt_response`` asks :func:`may_answer_prompt` before it consumes the
prompt, then dispatches through :data:`PROMPT_RESOLVERS` (``adapter`` is the ``RelayAdapter``).
"""

from __future__ import annotations

from typing import Any, Optional

# Prompt option id -> in-channel ack label (the option set doubles as the choice allowlist).
EXEC_APPROVAL_LABELS = {
    "once": "✅ Approved once",
    "session": "✅ Approved for session",
    "always": "✅ Approved permanently",
    "deny": "❌ Denied",
}
SLASH_CONFIRM_LABELS = {"once": "✅ Approved once", "always": "🔒 Always approve", "cancel": "❌ Cancelled"}


def may_answer_prompt(pending: dict[str, Any], answered_by: Optional[str], option_id: str = "") -> bool:
    """An exec-approval answer is ``/approve`` / ``/deny`` and a slash-confirm answer runs the
    confirmed command, so the presser must pass that slash policy; asked before the prompt is
    consumed (clarify: always)."""
    session_key = str(pending.get("session_key") or "")
    if pending.get("kind") == "exec_approval":
        from tools.approval import gateway_approval_refusal

        return gateway_approval_refusal(session_key, answered_by, option_id) is None
    if pending.get("kind") != "slash_confirm":
        return True
    from tools import slash_confirm as slash_confirm_mod

    return slash_confirm_mod.can_answer(session_key, answered_by)


async def resolve_exec_approval(adapter, state, option_id, chat_id, ack_meta) -> None:
    from tools.approval import resolve_gateway_approval_card
    choice = option_id if option_id in EXEC_APPROVAL_LABELS else "deny"
    count = resolve_gateway_approval_card(str(state.get("session_key") or ""), choice, state.get("request_id"))
    label = EXEC_APPROVAL_LABELS[choice] if count else "⌛ Approval expired — no command was waiting."
    # In-channel ack preserves the audit trail the native edit gives (the
    # connector's prompt message can't be edited cross-platform yet).
    adapter._send_lifecycle_ack(chat_id, label, ack_meta)
    if count:
        adapter.resume_typing_for_chat(chat_id)


async def resolve_slash_confirm(adapter, state, option_id, chat_id, ack_meta) -> None:
    from tools import slash_confirm as slash_confirm_mod

    choice = option_id if option_id in SLASH_CONFIRM_LABELS else "cancel"
    result_text = await slash_confirm_mod.resolve(
        str(state.get("session_key") or ""), str(state.get("confirm_id") or ""), choice,
        user_id=state.get("answered_by"),
    )
    adapter._send_lifecycle_ack(chat_id, SLASH_CONFIRM_LABELS[choice], ack_meta)
    if result_text:
        adapter._send_lifecycle_ack(chat_id, str(result_text), ack_meta)


async def resolve_clarify(adapter, state, option_id, chat_id, ack_meta) -> None:
    from tools.clarify_gateway import mark_awaiting_text, resolve_gateway_clarify

    clarify_id = str(state.get("clarify_id") or "")
    if option_id == "other":
        mark_awaiting_text(clarify_id)
        adapter._send_lifecycle_ack(chat_id, "✏️ Type your answer:", ack_meta)
        return
    choices = state.get("choices") or []
    try:
        idx = int(option_id[1:]) if option_id.startswith("c") else -1
    except ValueError:
        idx = -1
    if 0 <= idx < len(choices):
        resolve_gateway_clarify(clarify_id, str(choices[idx]))
        adapter._send_lifecycle_ack(chat_id, f"✅ {choices[idx]}", ack_meta)
    else:
        # Unmappable option: flip to text capture (never dead-end a clarify).
        mark_awaiting_text(clarify_id)


# prompt kind -> resolver (order-independent: kinds are distinct keys).
PROMPT_RESOLVERS = {
    "exec_approval": resolve_exec_approval,
    "slash_confirm": resolve_slash_confirm,
    "clarify": resolve_clarify,
}
