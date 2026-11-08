"""A native approval answer (button, reaction, card tap) is ``/approve`` / ``/deny``, so with
``allow_admin_from`` set it takes the same slash policy on every chat platform (#120506).

Each case registers a real pending approval and the gate the real ``TurnRunner`` registers for
its turn, drives the adapter's real click handler, and checks the approval queue: a non-admin's
click leaves the approval pending, the admin's click resolves it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry

ADMIN = "1001"
MEMBER = "2002"


def _runner(platform, admins):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={platform: PlatformConfig(
        enabled=True, token="***", extra={"allow_admin_from": list(admins)})})
    return runner


def _turn(platform, admins, session_key, chat_type="dm"):
    """The real ``TurnRunner`` for a turn raised by ``requester`` in a gated chat."""
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    real = _runner(platform, admins)
    runner = SimpleNamespace(session_authority=None, _consume_pending_native_image_paths=lambda _k: [],
                             _check_slash_access=real._check_slash_access, config=real.config)
    source = SessionSource(platform=platform, chat_id="c1", chat_type=chat_type, user_id="requester")
    return TurnRunner(runner, TurnContext(message="hi", session_id="s1", session_key=session_key, source=source))


@pytest.fixture
def pending(request):
    """``(entry, arm(platform, admins, ...))``: a real blocking approval plus the turn's gate."""
    keys = []

    def arm(platform, admins, session_key, chat_type="dm"):
        turn = _turn(platform, admins, session_key, chat_type)
        approval.register_gateway_notify(session_key, lambda _data: None, may_answer=turn._approval_answer_refusal)
        entry = _ApprovalEntry({"command": "rm -rf /srv/data"})
        approval._gateway_queues.setdefault(session_key, []).append(entry)
        keys.append(session_key)
        return entry

    yield arm
    for key in keys:
        approval.unregister_gateway_notify(key)


def test_turn_runner_registers_the_gate_for_its_turn_only():
    """Producer side: the turn registers the slash gate with its approval callback and drops it
    when the turn ends, so a host surface or a later ungated turn is never left gated."""
    key = "agent:main:telegram:dm:c1"
    turn = _turn(Platform.TELEGRAM, [ADMIN], key)
    seen = []

    def run(_message, **_kwargs):
        seen.append({uid: approval.gateway_approval_refusal(key, uid, "once") is None for uid in (ADMIN, MEMBER)})
        seen.append(approval.gateway_approval_refusal(key, MEMBER, "deny") is None)
        return {"final_response": "ok"}

    turn._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert seen == [{ADMIN: True, MEMBER: False}, False]
    assert approval.gateway_approval_refusal(key, MEMBER, "once") is None


def test_ungated_chat_keeps_every_admitted_member_answering(pending):
    """No ``allow_admin_from``: the gate is off, as before."""
    key = "agent:main:telegram:dm:open"
    entry = pending(Platform.TELEGRAM, [], key)
    assert approval.gateway_approval_refusal(key, MEMBER, "always") is None
    assert entry.result is None


def test_user_allowed_commands_lets_a_listed_member_approve():
    """``user_allowed_commands: [approve]`` admits a non-admin exactly as it admits ``/approve``."""
    key = "agent:main:telegram:dm:c1"
    turn = _turn(Platform.TELEGRAM, [ADMIN], key)
    turn._runner.config.platforms[Platform.TELEGRAM].extra["user_allowed_commands"] = ["approve"]
    assert turn._approval_answer_refusal(MEMBER, "approve") is None
    assert "/deny is admin-only" in turn._approval_answer_refusal(MEMBER, "deny")


# ── Telegram inline keyboard ─────────────────────────────────────────────


def test_telegram_button(pending, monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    key = "agent:main:telegram:dm:12345"
    entry = pending(Platform.TELEGRAM, [ADMIN], key)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = AsyncMock()
    adapter._approval_state[5] = key

    def click(uid):
        query = AsyncMock()
        query.data = "ea:always:5"
        query.message = MagicMock(chat_id=12345, message_thread_id=None)
        query.from_user = SimpleNamespace(id=int(uid), first_name=f"user{uid}")
        asyncio.run(adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock()))
        return query

    refused = click(MEMBER)
    assert entry.result is None and adapter._approval_state.get(5) == key
    refused.edit_message_text.assert_not_awaited()
    assert "/approve is admin-only" in refused.answer.await_args.kwargs["text"]
    click(ADMIN)
    assert entry.result == "always"


# ── Slack Block Kit ──────────────────────────────────────────────────────


def test_slack_button(pending):
    from tests.gateway.test_slack_approval_buttons import _attach_auth_runner, _make_adapter

    key = "agent:main:slack:dm:C1"
    entry = pending(Platform.SLACK, ["U_ADMIN"], key)
    adapter = _make_adapter()
    _attach_auth_runner(adapter)
    adapter._team_clients["T1"].chat_update = AsyncMock()
    adapter._approval_resolved["1.2"] = False

    def click(uid):
        body = {"message": {"ts": "1.2", "blocks": []}, "channel": {"id": "C1"}, "user": {"name": uid, "id": uid}}
        asyncio.run(adapter._handle_approval_action(AsyncMock(), body, {"action_id": "hermes_approve_once", "value": key}))

    click("U_MEMBER")
    assert entry.result is None and "1.2" in adapter._approval_resolved
    click("U_ADMIN")
    assert entry.result == "once"


# ── Discord button view ──────────────────────────────────────────────────


def test_discord_button(pending, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    key = "agent:main:discord:dm:555"
    entry = pending(Platform.DISCORD, [ADMIN], key)

    async def scenario():
        adapter = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token="***"))
        message = SimpleNamespace(id=1, edit=AsyncMock(), embeds=[])
        sent = {}

        async def send(**kwargs):
            sent.update(kwargs)
            message.embeds = [kwargs["embed"]]
            return message

        adapter._client = SimpleNamespace(get_channel=lambda _id: SimpleNamespace(
            send=send, get_partial_message=lambda _id: message))
        await adapter.send_exec_approval(chat_id="555", command="rm -rf /srv/data", session_key=key)
        view = sent["view"]
        monkeypatch.setattr(view, "_gate", AsyncMock(return_value=True))  # admission is not under test

        def interaction(uid):
            return SimpleNamespace(message=message, user=SimpleNamespace(id=int(uid), display_name=uid),
                                   response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))

        refused = interaction(MEMBER)
        await view._resolve(refused, "always", 2, "platform.discord.approval.resolved_always")
        assert entry.result is None and not view.resolved
        assert "/approve is admin-only" in refused.response.send_message.await_args.args[0]
        await view._resolve(interaction(ADMIN), "once", 2, "platform.discord.approval.resolved_once")
        assert entry.result == "once"

    asyncio.run(scenario())


# ── Matrix reactions ─────────────────────────────────────────────────────


def test_matrix_reaction(pending, monkeypatch):
    from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

    admin, member = "@admin:example.org", "@member:example.org"
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", f"{admin},{member}")
    key = "agent:main:matrix:dm:!room:example.org"
    entry = pending(Platform.MATRIX, [admin], key)
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
    adapter._user_id = "@bot:example.org"
    adapter._approval_require_sender = False  # the opt-out that let any room member answer
    adapter._send_invalid_reaction_feedback = AsyncMock()
    adapter._redact_bot_approval_reactions = AsyncMock()
    adapter._approval_prompts_by_event["$card"] = _MatrixApprovalPrompt(
        session_key=key, chat_id="!room:example.org", message_id="$card")

    def react(sender):
        asyncio.run(adapter._on_reaction(SimpleNamespace(
            sender=sender, event_id=f"$r-{sender}", room_id="!room:example.org",
            content={"m.relates_to": {"event_id": "$card", "key": "♾️"}})))

    react(member)
    assert entry.result is None and "$card" in adapter._approval_prompts_by_event
    adapter._send_invalid_reaction_feedback.assert_awaited_once()
    react(admin)
    assert entry.result == "always"


# ── Feishu card ──────────────────────────────────────────────────────────


def test_feishu_card(pending):
    from tests.gateway.test_feishu_approval_buttons import _make_adapter

    key = "agent:main:feishu:dm:oc_1"
    entry = pending(Platform.FEISHU, ["ou_admin"], key)
    adapter = _make_adapter()
    adapter.set_authorization_check(lambda *_a, **_kw: True)
    adapter._approval_state[1] = {"session_key": key, "message_id": "m1", "chat_id": "oc_1"}

    asyncio.run(adapter._resolve_approval(1, "once", "M", open_id="ou_member", chat_id="oc_1"))
    assert entry.result is None and 1 in adapter._approval_state
    asyncio.run(adapter._resolve_approval(1, "once", "A", open_id="ou_admin", chat_id="oc_1"))
    assert entry.result == "once"


# ── Microsoft Teams adaptive card ────────────────────────────────────────


def test_teams_card(pending):
    from tests.gateway.test_teams import TeamsAdapter, _make_config

    key = "agent:main:teams:dm:c1"
    entry = pending(Platform("teams"), ["aad-admin"], key)
    adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
    adapter._app, adapter._card_action_denied, adapter._invoke_card = object(), (lambda _f: None), (lambda body: body)

    def click(uid):
        ctx = MagicMock()
        ctx.activity.value.action.data = {"hermes_action": "approve_always", "session_key": key}
        ctx.activity.from_ = SimpleNamespace(aad_object_id=uid, id=uid)
        return asyncio.run(adapter._on_card_action(ctx))

    click("aad-member")
    assert entry.result is None
    click("aad-admin")
    assert entry.result == "always"


# ── WhatsApp Cloud reply buttons ─────────────────────────────────────────


def test_whatsapp_cloud_tap(pending, monkeypatch):
    from tests.gateway.test_whatsapp_cloud import _make_adapter, _mock_httpx_response

    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")
    key = "agent:main:whatsapp_cloud:dm:15550001001"
    entry = pending(Platform.WHATSAPP_CLOUD, ["15550001001"], key)
    adapter = _make_adapter()
    adapter._http_client = MagicMock()
    adapter._http_client.post = AsyncMock(return_value=_mock_httpx_response(200, {"messages": [{"id": "x"}]}))
    adapter._exec_approval_state["a1"] = key

    def tap(sender):
        raw = {"from": sender, "type": "interactive",
               "interactive": {"type": "button_reply", "button_reply": {"id": "appr:a1:approve", "title": "Approve"}}}
        return asyncio.run(adapter._dispatch_interactive_reply(raw, {}))

    assert tap("15550002002") is True  # claimed, never re-dispatched as the text "Approve"
    assert entry.result is None and adapter._exec_approval_state.get("a1") == key
    tap("15550001001")
    assert entry.result == "approve"


# ── QQ Bot keyboard ──────────────────────────────────────────────────────


@pytest.mark.parametrize("admins, resolves", [(["u-42"], True), (["someone-else"], False)])
def test_qqbot_button(pending, admins, resolves):
    from plugins.platforms.qqbot.adapter import QQAdapter
    from plugins.platforms.qqbot.keyboards import parse_interaction_event

    key = "agent:main:qqbot:dm:u-42"
    entry = pending(Platform.QQBOT, admins, key)
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={}))
    event = parse_interaction_event({"id": "i", "chat_type": 2, "user_openid": "u-42",
                                     "data": {"resolved": {"button_data": f"approve:{key}:allow-once"}}})
    asyncio.run(adapter._default_interaction_dispatch(event))
    assert (entry.result == "once") is resolves


# ── Relay connector prompts ──────────────────────────────────────────────


def test_relay_prompt_response(pending):
    from tests.gateway.relay.test_relay_interactive import _adapter

    key = "agent:main:telegram:dm:c1"
    entry = pending(Platform.TELEGRAM, [ADMIN], key)
    adapter, stub = _adapter()

    def press(uid):
        event = MessageEvent(text="/always", message_type=MessageType.COMMAND,
                             source=SessionSource(platform="telegram", chat_id="c1", chat_type="dm", user_id=uid),
                             prompt_response={"prompt_id": prompt_id, "option_id": "always"})
        assert asyncio.run(adapter._consume_prompt_response(event)) is True

    asyncio.run(adapter.send_exec_approval("c1", "rm -rf /srv/data", key))
    prompt_id = stub.sent[-1]["prompt_id"]
    press(MEMBER)
    assert entry.result is None and prompt_id in adapter._pending_prompts
    press(ADMIN)
    assert entry.result == "always"
