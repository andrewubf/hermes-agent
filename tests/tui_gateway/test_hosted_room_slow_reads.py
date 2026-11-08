"""The supervisor keeps a watched turn leased while its worker waits on I/O."""

import sqlite3
import threading
import time

import pytest

from gateway import hosted_room_driver as state
from gateway import hosted_rooms
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
from tests.tui_gateway.test_hosted_room_driver_runtime import (
    BINDING, FakeSessionRPC, RecordingTurnLocks, _admit, _identity, db as db_fixture,
)
from tests.waiting import wait_until

db = db_fixture


def _member_runtime(db, clock, *, member_transport, auto_complete=False, rooms=(BINDING,), member=None, **kwargs):
    member = member or FakeSessionRPC(auto_complete=auto_complete)
    return member, HostedRoomRuntime(
        db_path=db, rooms=rooms, rpc=None if member_transport else member,
        transport_resolver=(lambda _binding, _task: member) if member_transport else None,
        turn_lock=RecordingTurnLocks(), clock=clock, max_concurrent_rooms=1, **kwargs)


def _restarted_mid_turn(db, monkeypatch, clock, **kwargs):
    """The queued turn runs under a process that dies, taking its callback along. Returns the bot and
    the next process's runtime on the gateway route, which leaves a watch after one poll."""
    member, first = _member_runtime(db, clock, member_transport=True)
    monkeypatch.setattr(first._wake, 'wait', lambda _timeout: first._stop.set())
    first._run_room_once(BINDING)
    next(iter(member.states.values()))['on_terminal'] = None
    _member, runtime = _member_runtime(db, clock, member_transport=True, member=member, **kwargs)
    monkeypatch.setattr(runtime._wake, 'wait', lambda _timeout: runtime._stop.set())
    return member, runtime


def _supervisor_cycle(runtime, monkeypatch):
    # Run the existing supervisor with its sole worker slot occupied by the blocked read.
    with monkeypatch.context() as patch:
        patch.setattr(runtime, '_thread', threading.current_thread())
        patch.setattr(runtime, '_room_threads', {BINDING.room_id: threading.current_thread()})
        runtime._run_cycle()


@pytest.mark.parametrize('member_transport', [True, False])
@pytest.mark.parametrize('stop_during_read', [False, True])
def test_one_timed_out_read_does_not_mark_a_live_turn_indeterminate(
        db, monkeypatch, member_transport, stop_during_read):
    identity, now = _identity(), [100.0]
    _admit(db, identity)
    member, runtime = _member_runtime(db, lambda: now[0], member_transport=member_transport)
    reads = []

    def slow_first(read):
        def observe(**kwargs):
            reads.append(kwargs)
            if len(reads) == 1:
                for _ in range(2):
                    now[0] += 15
                    _supervisor_cycle(runtime, monkeypatch)
                if stop_during_read:
                    runtime.cancel(identity, cancel_id='stop-during-read')
                raise TimeoutError('transport call timed out after 30 seconds')
            return read(**kwargs)
        return observe

    # Whichever read the route observes with first waits out its whole transport timeout.
    member.history = slow_first(member.history)
    # Yield after the failed read to check the next worker cycle, without waiting in real time.
    monkeypatch.setattr(runtime._wake, 'wait', lambda _timeout: runtime._stop.set())
    runtime._process_room(BINDING)
    runtime._stop.clear()
    runtime._process_room(BINDING)

    task = state.get_task(db, identity)
    assert task['status'] == ('cancelled' if stop_during_read else 'running')
    if not stop_during_read:
        assert next(iter(member.states.values()))['active'] is True
        member.complete(identity.task_id, content='Adjustment applied once.')
        assert state.get_task(db, identity)['status'] == 'settled'
    assert len([call for call, _ in member.calls if call == 'submit']) == 1


@pytest.mark.parametrize('takeover', ['restart', 'another_gateway', 'unwatched'])
def test_supervisor_cannot_renew_a_lease_taken_over_by_another_owner(db, monkeypatch, takeover):
    identity, now = _identity(), [100.0]
    _admit(db, identity)
    runtime = HostedRoomRuntime(
        db_path=db, rooms=[BINDING], rpc=FakeSessionRPC(),
        turn_lock=RecordingTurnLocks(), clock=lambda: now[0], max_concurrent_rooms=1)
    old = runtime._ensure_lease(BINDING)
    state.start_task(db, identity, old, expected_cancel_generation=0, clock=runtime.clock)
    if takeover != 'unwatched':
        runtime._current_tasks[BINDING.room_id] = identity
    now[0] += 31
    gateway_id = BINDING.gateway_id
    epoch = BINDING.authority_epoch
    if takeover == 'another_gateway':
        gateway_id, epoch = 'gateway-b', 2
        hosted_rooms.claim_authority(
            db, room_id=BINDING.room_id, expected_gateway_id=BINDING.gateway_id,
            expected_epoch=1, new_gateway_id=gateway_id, event_id='transfer', now=now[0])
    new = state.acquire_lease(
        db, room_id=BINDING.room_id, gateway_id=gateway_id, authority_epoch=epoch,
        process_generation=runtime.process_generation if takeover == 'unwatched' else 'replacement',
        ttl_seconds=30, clock=runtime.clock)
    _supervisor_cycle(runtime, monkeypatch)
    state.recover_room(db, new, clock=runtime.clock)
    assert state.get_task(db, identity)['status'] == 'indeterminate'
    with pytest.raises(state.StaleLeaseError):
        state.renew_lease(db, old, ttl_seconds=30, clock=runtime.clock)


@pytest.mark.parametrize('member_transport', [True, False])
def test_a_submit_that_times_out_does_not_hold_the_group_chat(db, monkeypatch, member_transport):
    """A timed-out submit may or may not have reached the bot. The supervisor renewed the room's
    lease meanwhile, so waiting for it to lapse cannot fence the attempt: the next message waits."""
    identity, later, now = _identity(), _identity('task-2'), [100.0]
    _admit(db, identity)
    _admit(db, later)
    member, runtime = _member_runtime(db, lambda: now[0], member_transport=member_transport)
    submit = member.submit

    def timed_out_submit(**kwargs):
        if kwargs['task'] == later:
            return submit(**kwargs)
        for _ in range(2):  # an owner-socket or loop timeout, renewed by the supervisor meanwhile
            now[0] += 15
            _supervisor_cycle(runtime, monkeypatch)
        raise TimeoutError('transport call timed out after 30 seconds')

    member.submit = timed_out_submit
    monkeypatch.setattr(runtime._wake, 'wait', lambda _timeout: runtime._stop.set())
    runtime._process_room(BINDING)
    runtime._stop.clear()
    for _ in range(30):  # the room is rescheduled as soon as the worker leaves, then every 5 s
        runtime._run_room_once(BINDING)
        now[0] += 5
    assert state.get_task(db, identity)['status'] == 'deferred'
    assert state.get_task(db, later)['status'] == 'running'
    assert [call['task'] for name, call in member.calls if name == 'submit'] == [later]


@pytest.mark.parametrize('member_transport', [True, False])
def test_supervisor_renewing_during_a_stuck_submit_still_frees_the_group_chat(db, member_transport):
    """The real supervisor thread renews the lease while the worker thread is stuck in submit."""
    identity, later = _identity(), _identity('task-2')
    _admit(db, identity)
    _admit(db, later)
    member, runtime = _member_runtime(
        db, time.time, member_transport=member_transport, auto_complete=True, lease_ttl_seconds=3, poll_interval_seconds=0.01,
        active_poll_interval_seconds=0.01, indeterminate_defer_seconds=0.2)
    submit, renewed_during_submit = member.submit, threading.Event()

    def stuck_submit(**kwargs):
        if kwargs['task'] == later:
            return submit(**kwargs)
        with sqlite3.connect(db) as conn:
            def lease_expiry():
                return conn.execute('SELECT expires_at FROM hosted_room_driver_leases').fetchone()[0]
            first_expiry = lease_expiry()
            wait_until(lambda: lease_expiry() > first_expiry, timeout=10, what='a supervisor renewal')
        renewed_during_submit.set()
        raise TimeoutError('transport call timed out')

    member.submit = stuck_submit
    runtime.start()
    try:
        wait_until(lambda: state.get_task(db, later)['status'] == 'settled', timeout=10,
                   what='the next message to run after the timed-out submit')
    finally:
        assert runtime.stop(timeout=10)
    assert renewed_during_submit.is_set()
    assert state.get_task(db, identity)['status'] == 'deferred'


def _locked_once(monkeypatch, name):
    """Make one durable write fail the way a busy SQLite writer does."""
    write, locked = getattr(state, name), []

    def locked_once(*args, **kwargs):
        if not locked:
            locked.append(True)
            raise sqlite3.OperationalError('database is locked')
        return write(*args, **kwargs)

    monkeypatch.setattr(state, name, locked_once)


@pytest.mark.parametrize('member_transport', [True, False])
def test_a_locked_database_while_fencing_a_failed_submit_does_not_hold_the_group_chat_for_good(
        db, monkeypatch, member_transport):
    identity, later, now = _identity(), _identity('task-2'), [100.0]
    _admit(db, identity)
    _admit(db, later)
    member, runtime = _member_runtime(db, lambda: now[0], member_transport=member_transport)
    monkeypatch.setattr(runtime._wake, 'wait', lambda _timeout: runtime._stop.set())
    submit = member.submit

    def timed_out_submit(**kwargs):
        if kwargs['task'] == later:
            return submit(**kwargs)
        raise TimeoutError('transport call timed out after 30 seconds')

    member.submit = timed_out_submit
    _locked_once(monkeypatch, 'mark_attempt_indeterminate')
    for _ in range(14):
        runtime._stop.clear()
        runtime._run_room_once(BINDING)
        now[0] += 5
    assert state.get_task(db, identity)['status'] == 'deferred'
    assert state.get_task(db, later)['status'] == 'running'


def test_a_locked_lease_renewal_while_watching_does_not_hold_the_group_chat_for_good(db, monkeypatch):
    identity, now = _identity(), [100.0]
    _admit(db, identity)
    _member, runtime = _member_runtime(db, lambda: now[0], member_transport=True)
    monkeypatch.setattr(runtime._wake, 'wait', lambda _timeout: now.__setitem__(0, now[0] + 16))
    _locked_once(monkeypatch, 'renew_lease')
    _locked_once(monkeypatch, 'mark_attempt_indeterminate')
    runtime._run_room_once(BINDING)  # the renewal raises out of the watch, and so does its fence
    assert state.get_task(db, identity)['status'] == 'running'
    runtime._run_room_once(BINDING)
    assert state.get_task(db, identity)['status'] == 'indeterminate'


def test_another_pass_leaves_an_attempt_that_a_worker_is_watching(db, monkeypatch):
    identity, now = _identity(), [100.0]
    _admit(db, identity)
    _member, runtime = _member_runtime(db, lambda: now[0], member_transport=True)
    seen = []

    def second_pass(_timeout):
        thread = threading.Thread(target=runtime._run_room_once, args=(BINDING,))
        thread.start()
        thread.join(10)
        seen.append(state.get_task(db, identity)['status'])
        runtime._stop.set()

    monkeypatch.setattr(runtime._wake, 'wait', second_pass)
    runtime._run_room_once(BINDING)
    assert seen == ['running']
    assert state.get_task(db, identity)['status'] == 'running'


def test_one_room_whose_lease_renewal_fails_does_not_stop_the_others(db, monkeypatch):
    other = HostedRoomBinding('room-2', BINDING.gateway_id, 1)
    hosted_rooms.create_room(db, room_id=other.room_id, name='Other room', members=[
        {'profile': 'ops', 'handle': 'ops'}], authority_gateway_id=BINDING.gateway_id)
    now = [100.0]
    _member, runtime = _member_runtime(db, lambda: now[0], member_transport=False, rooms=(BINDING, other))
    leases = {binding.room_id: runtime._ensure_lease(binding) for binding in (BINDING, other)}
    runtime._current_tasks.update({BINDING.room_id: _identity(), other.room_id: _identity('task-2')})
    renew = state.renew_lease

    def locked_for_one_room(db_path, lease, **kwargs):
        if lease.room_id == BINDING.room_id:
            raise sqlite3.OperationalError('database is locked')
        return renew(db_path, lease, **kwargs)

    monkeypatch.setattr(state, 'renew_lease', locked_for_one_room)
    now[0] += 20
    _supervisor_cycle(runtime, monkeypatch)
    assert runtime._leases[other.room_id].expires_at > leases[other.room_id].expires_at
    assert 'database is locked' in runtime.status()['last_error']
