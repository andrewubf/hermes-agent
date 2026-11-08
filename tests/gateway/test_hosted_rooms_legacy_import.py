"""Legacy Group Chat migration preserves the configured policy replay capacity (#149447)."""

from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint


def test_pre_isolation_rooms_keep_policy_replay_capacity(tmp_path):
    legacy, shared = tmp_path / "state.db", tmp_path / "shared-state.db"
    hosted_rooms.create_room(
        legacy,
        room_id="pre-isolation-room",
        name="Pre-isolation room",
        members=[
            {"member_id": "a", "profile": "default", "handle": "a"},
            {"member_id": "b", "profile": "default", "handle": "b"},
        ],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )
    assert HostedRoomPolicyCheckpoint(legacy, max_active_events=128).max_active_events == 128

    assert [room["room_id"] for room in hosted_rooms.list_rooms(shared)] == ["pre-isolation-room"]
    assert HostedRoomPolicyCheckpoint(shared).max_active_events == 128
