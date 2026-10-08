import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert

from app.models import conversation_members, conversations
from app.services.messages.calls import MemoryCallStore, handle_call_signal
from tests.conftest import seed_user


def _seed_direct(db, left, right):
    now = datetime.now(UTC)
    conversation_id = uuid4()
    db.execute(
        insert(conversations).values(
            id=conversation_id,
            kind="direct",
            title=None,
            created_by=left,
            created_at=now,
            updated_at=now,
        )
    )
    for user_id in (left, right):
        db.execute(
            insert(conversation_members).values(
                conversation_id=conversation_id,
                user_id=user_id,
                joined_at=now,
            )
        )
    return conversation_id


def _signal(call_id, conversation_id, signal_type):
    return {
        "callId": str(call_id),
        "conversationId": str(conversation_id),
        "type": signal_type,
        "payload": {},
    }


def test_non_member_cannot_signal(db_transaction):
    asyncio.run(_non_member_cannot_signal(db_transaction))


async def _non_member_cannot_signal(db_transaction):
    caller_id, _ = seed_user(db_transaction, username_prefix="call-caller")
    callee_id, _ = seed_user(db_transaction, username_prefix="call-callee")
    outsider_id, _ = seed_user(db_transaction, username_prefix="call-out")
    conversation_id = _seed_direct(db_transaction, caller_id, callee_id)
    store = MemoryCallStore()
    await store.join_presence(str(callee_id))

    deliveries = await handle_call_signal(
        db_transaction,
        store,
        outsider_id,
        _signal(uuid4(), conversation_id, "invite"),
    )

    assert [item.envelope["type"] for item in deliveries] == ["error"]
    assert deliveries[0].envelope["payload"]["code"] == "forbidden"
    assert deliveries[0].user_id == outsider_id
    assert store.sessions == {}


def test_invite_accept_hangup(db_transaction):
    asyncio.run(_invite_accept_hangup(db_transaction))


async def _invite_accept_hangup(db_transaction):
    caller_id, caller_name = seed_user(db_transaction, username_prefix="call-a")
    callee_id, _ = seed_user(db_transaction, username_prefix="call-b")
    conversation_id = _seed_direct(db_transaction, caller_id, callee_id)
    store = MemoryCallStore()
    await store.join_presence(str(caller_id))
    await store.join_presence(str(callee_id))
    call_id = uuid4()

    invited = await handle_call_signal(
        db_transaction,
        store,
        caller_id,
        _signal(call_id, conversation_id, "invite"),
    )
    assert len(invited) == 1
    assert invited[0].user_id == callee_id
    assert invited[0].envelope["type"] == "invite"
    assert invited[0].envelope["fromUserId"] == str(caller_id)
    assert invited[0].envelope["fromUsername"] == caller_name
    assert store.sessions[str(call_id)]["state"] == "ringing"

    accepted = await handle_call_signal(
        db_transaction,
        store,
        callee_id,
        _signal(call_id, conversation_id, "accept"),
    )
    assert accepted[0].user_id == caller_id
    assert accepted[0].envelope["type"] == "accept"
    assert store.sessions[str(call_id)]["state"] == "active"

    hung_up = await handle_call_signal(
        db_transaction,
        store,
        caller_id,
        _signal(call_id, conversation_id, "hangup"),
    )
    assert hung_up[0].user_id == callee_id
    assert hung_up[0].envelope["type"] == "hangup"
    assert str(call_id) not in store.sessions
    assert str(conversation_id) not in store.conversations


def test_second_invite_while_ringing_is_busy(db_transaction):
    asyncio.run(_second_invite_while_ringing_is_busy(db_transaction))


async def _second_invite_while_ringing_is_busy(db_transaction):
    caller_id, _ = seed_user(db_transaction, username_prefix="call-busy-a")
    callee_id, _ = seed_user(db_transaction, username_prefix="call-busy-b")
    conversation_id = _seed_direct(db_transaction, caller_id, callee_id)
    store = MemoryCallStore()
    await store.join_presence(str(callee_id))

    first = await handle_call_signal(
        db_transaction,
        store,
        caller_id,
        _signal(uuid4(), conversation_id, "invite"),
    )
    assert first[0].envelope["type"] == "invite"

    second = await handle_call_signal(
        db_transaction,
        store,
        caller_id,
        _signal(uuid4(), conversation_id, "invite"),
    )
    assert second[0].user_id == caller_id
    assert second[0].envelope["type"] == "busy"
    assert len(store.sessions) == 1
