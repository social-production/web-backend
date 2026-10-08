"""Direct-call signaling. Audio stays on the devices; this only tracks the session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import users
from app.services.messages.conversations import (
    _ensure_member,
    _get_conversation_participants,
    _get_conversation_row,
)

RING_TTL_SECONDS = 45
ACTIVE_TTL_SECONDS = 4 * 60 * 60
PRESENCE_TTL_SECONDS = 25
MAX_PAYLOAD_CHARS = 65536

CLIENT_SIGNAL_TYPES = frozenset({"invite", "accept", "reject", "offer", "answer", "ice", "hangup"})


@dataclass(frozen=True)
class CallDelivery:
    user_id: UUID
    envelope: dict[str, object]


class CallStore(Protocol):
    async def is_present(self, user_id: str) -> bool: ...

    async def claim_conversation(self, conversation_id: str, call_id: str, ttl: int) -> bool: ...

    async def touch_conversation(self, conversation_id: str, call_id: str, ttl: int) -> None: ...

    async def release_conversation(self, conversation_id: str, call_id: str) -> None: ...

    async def save_session(self, session: dict[str, str], ttl: int) -> None: ...

    async def get_session(self, call_id: str) -> dict[str, str] | None: ...

    async def delete_session(self, call_id: str) -> None: ...


class MemoryCallStore:
    """In-process session used by tests. Production uses RedisCallStore."""

    def __init__(self) -> None:
        self.presence: dict[str, int] = {}
        self.sessions: dict[str, dict[str, str]] = {}
        self.conversations: dict[str, str] = {}

    async def join_presence(self, user_id: str) -> None:
        self.presence[user_id] = self.presence.get(user_id, 0) + 1

    async def refresh_presence(self, user_id: str) -> None:
        if self.presence.get(user_id, 0) <= 0:
            self.presence[user_id] = 1

    async def leave_presence(self, user_id: str) -> None:
        remaining = self.presence.get(user_id, 0) - 1
        if remaining <= 0:
            self.presence.pop(user_id, None)
        else:
            self.presence[user_id] = remaining

    async def is_present(self, user_id: str) -> bool:
        return self.presence.get(user_id, 0) > 0

    async def claim_conversation(self, conversation_id: str, call_id: str, ttl: int) -> bool:
        del ttl
        if conversation_id in self.conversations:
            return False
        self.conversations[conversation_id] = call_id
        return True

    async def touch_conversation(self, conversation_id: str, call_id: str, ttl: int) -> None:
        del ttl
        if self.conversations.get(conversation_id) == call_id:
            self.conversations[conversation_id] = call_id

    async def release_conversation(self, conversation_id: str, call_id: str) -> None:
        if self.conversations.get(conversation_id) == call_id:
            del self.conversations[conversation_id]

    async def save_session(self, session: dict[str, str], ttl: int) -> None:
        del ttl
        self.sessions[session["callId"]] = dict(session)

    async def get_session(self, call_id: str) -> dict[str, str] | None:
        session = self.sessions.get(call_id)
        return dict(session) if session else None

    async def delete_session(self, call_id: str) -> None:
        self.sessions.pop(call_id, None)


class RedisCallStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    def channel(self, user_id: str) -> str:
        return f"call:user:{user_id}"

    def _presence_key(self, user_id: str) -> str:
        return f"call:presence:{user_id}"

    def _session_key(self, call_id: str) -> str:
        return f"call:session:{call_id}"

    def _conversation_key(self, conversation_id: str) -> str:
        return f"call:conv:{conversation_id}"

    async def join_presence(self, user_id: str) -> None:
        key = self._presence_key(user_id)
        await self.redis.incr(key)
        await self.redis.expire(key, PRESENCE_TTL_SECONDS)

    async def refresh_presence(self, user_id: str) -> None:
        key = self._presence_key(user_id)
        if await self.redis.exists(key):
            await self.redis.expire(key, PRESENCE_TTL_SECONDS)
            return
        await self.join_presence(user_id)

    async def leave_presence(self, user_id: str) -> None:
        key = self._presence_key(user_id)
        remaining = int(await self.redis.decr(key))
        if remaining <= 0:
            await self.redis.delete(key)

    async def is_present(self, user_id: str) -> bool:
        raw = await self.redis.get(self._presence_key(user_id))
        try:
            return int(raw or 0) > 0
        except (TypeError, ValueError):
            return False

    async def publish(self, user_id: str, envelope: dict[str, object]) -> None:
        await self.redis.publish(self.channel(user_id), json.dumps(envelope))

    async def claim_conversation(self, conversation_id: str, call_id: str, ttl: int) -> bool:
        claimed = await self.redis.set(
            self._conversation_key(conversation_id), call_id, ex=ttl, nx=True
        )
        return bool(claimed)

    async def touch_conversation(self, conversation_id: str, call_id: str, ttl: int) -> None:
        key = self._conversation_key(conversation_id)
        current = await self.redis.get(key)
        if current == call_id:
            await self.redis.expire(key, ttl)

    async def release_conversation(self, conversation_id: str, call_id: str) -> None:
        key = self._conversation_key(conversation_id)
        current = await self.redis.get(key)
        if current == call_id:
            await self.redis.delete(key)

    async def save_session(self, session: dict[str, str], ttl: int) -> None:
        await self.redis.set(self._session_key(session["callId"]), json.dumps(session), ex=ttl)

    async def get_session(self, call_id: str) -> dict[str, str] | None:
        raw = await self.redis.get(self._session_key(call_id))
        if not raw:
            return None
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            return None
        return {str(key): str(value) for key, value in loaded.items()}

    async def delete_session(self, call_id: str) -> None:
        await self.redis.delete(self._session_key(call_id))


def _parse_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _username(db: Session, user_id: UUID) -> str:
    username = db.execute(
        select(users.c.username).where(users.c.id == user_id)
    ).scalar_one_or_none()
    return str(username or "")


def _envelope(
    *,
    call_id: str,
    conversation_id: str,
    signal_type: str,
    sender_id: UUID,
    sender_name: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "callId": call_id,
        "conversationId": conversation_id,
        "type": signal_type,
        "payload": payload or {},
        "fromUserId": str(sender_id),
        "fromUsername": sender_name,
    }


def _error(sender_id: UUID, call_id: str, conversation_id: str, code: str) -> list[CallDelivery]:
    return [
        CallDelivery(
            user_id=sender_id,
            envelope=_envelope(
                call_id=call_id,
                conversation_id=conversation_id,
                signal_type="error",
                sender_id=sender_id,
                sender_name="",
                payload={"code": code},
            ),
        )
    ]


def _direct_peer(db: Session, conversation_id: UUID, user_id: UUID) -> UUID:
    row = _get_conversation_row(db, conversation_id)
    if row["kind"] != "direct":
        raise HTTPException(status_code=422, detail="Calls are only available in direct chats")
    _ensure_member(db, conversation_id, user_id)
    others = [
        participant["id"]
        for participant in _get_conversation_participants(db, conversation_id)
        if participant["id"] != user_id
    ]
    if len(others) != 1 or not isinstance(others[0], UUID):
        raise HTTPException(status_code=422, detail="Calls are only available in direct chats")
    return others[0]


def _membership_code(exc: HTTPException) -> str:
    if exc.status_code == 403:
        return "forbidden"
    if exc.status_code == 404:
        return "not_found"
    return "not_direct"


async def _end_call(
    store: CallStore,
    session: dict[str, str],
    *,
    sender_id: UUID,
    sender_name: str,
    signal_type: str,
    peer_id: UUID,
) -> list[CallDelivery]:
    await store.delete_session(session["callId"])
    await store.release_conversation(session["conversationId"], session["callId"])
    return [
        CallDelivery(
            user_id=peer_id,
            envelope=_envelope(
                call_id=session["callId"],
                conversation_id=session["conversationId"],
                signal_type=signal_type,
                sender_id=sender_id,
                sender_name=sender_name,
            ),
        )
    ]


async def handle_call_signal(
    db: Session,
    store: CallStore,
    user_id: UUID,
    raw: dict[str, object],
) -> list[CallDelivery]:
    signal_type = raw.get("type")
    call_id = raw.get("callId") if isinstance(raw.get("callId"), str) else ""
    conversation_uuid = _parse_uuid(raw.get("conversationId"))
    conversation_id = str(conversation_uuid or "")
    if (
        signal_type not in CLIENT_SIGNAL_TYPES
        or not _parse_uuid(call_id)
        or conversation_uuid is None
    ):
        return _error(user_id, call_id, conversation_id, "invalid")

    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if len(json.dumps(payload)) > MAX_PAYLOAD_CHARS:
        return _error(user_id, call_id, conversation_id, "invalid")

    try:
        peer_id = _direct_peer(db, conversation_uuid, user_id)
    except HTTPException as exc:
        return _error(user_id, call_id, conversation_id, _membership_code(exc))

    sender_name = _username(db, user_id)
    peer_key = str(peer_id)

    if signal_type == "invite":
        if not await store.is_present(peer_key):
            return [
                CallDelivery(
                    user_id=user_id,
                    envelope=_envelope(
                        call_id=call_id,
                        conversation_id=conversation_id,
                        signal_type="unavailable",
                        sender_id=user_id,
                        sender_name=sender_name,
                    ),
                )
            ]
        claimed = await store.claim_conversation(conversation_id, call_id, RING_TTL_SECONDS)
        if not claimed:
            return [
                CallDelivery(
                    user_id=user_id,
                    envelope=_envelope(
                        call_id=call_id,
                        conversation_id=conversation_id,
                        signal_type="busy",
                        sender_id=user_id,
                        sender_name=sender_name,
                    ),
                )
            ]
        if not await store.is_present(peer_key):
            await store.release_conversation(conversation_id, call_id)
            return [
                CallDelivery(
                    user_id=user_id,
                    envelope=_envelope(
                        call_id=call_id,
                        conversation_id=conversation_id,
                        signal_type="unavailable",
                        sender_id=user_id,
                        sender_name=sender_name,
                    ),
                )
            ]
        session = {
            "callId": call_id,
            "conversationId": conversation_id,
            "callerId": str(user_id),
            "calleeId": peer_key,
            "state": "ringing",
        }
        await store.save_session(session, RING_TTL_SECONDS)
        return [
            CallDelivery(
                user_id=peer_id,
                envelope=_envelope(
                    call_id=call_id,
                    conversation_id=conversation_id,
                    signal_type="invite",
                    sender_id=user_id,
                    sender_name=sender_name,
                ),
            )
        ]

    session = await store.get_session(call_id)
    if session is None or session.get("conversationId") != conversation_id:
        return _error(user_id, call_id, conversation_id, "no_call")

    caller_id = _parse_uuid(session.get("callerId"))
    callee_id = _parse_uuid(session.get("calleeId"))
    if caller_id is None or callee_id is None or user_id not in {caller_id, callee_id}:
        return _error(user_id, call_id, conversation_id, "forbidden")

    other_id = callee_id if user_id == caller_id else caller_id

    if signal_type == "accept":
        if user_id != callee_id or session.get("state") != "ringing":
            return _error(user_id, call_id, conversation_id, "forbidden")
        session["state"] = "active"
        await store.save_session(session, ACTIVE_TTL_SECONDS)
        await store.touch_conversation(conversation_id, call_id, ACTIVE_TTL_SECONDS)
        return [
            CallDelivery(
                user_id=caller_id,
                envelope=_envelope(
                    call_id=call_id,
                    conversation_id=conversation_id,
                    signal_type="accept",
                    sender_id=user_id,
                    sender_name=sender_name,
                ),
            )
        ]

    if signal_type == "reject":
        if user_id != callee_id or session.get("state") != "ringing":
            return _error(user_id, call_id, conversation_id, "forbidden")
        return await _end_call(
            store,
            session,
            sender_id=user_id,
            sender_name=sender_name,
            signal_type="reject",
            peer_id=caller_id,
        )

    if signal_type == "hangup":
        return await _end_call(
            store,
            session,
            sender_id=user_id,
            sender_name=sender_name,
            signal_type="hangup",
            peer_id=other_id,
        )

    if session.get("state") != "active":
        return _error(user_id, call_id, conversation_id, "not_active")

    if signal_type == "offer" and user_id != caller_id:
        return _error(user_id, call_id, conversation_id, "forbidden")
    if signal_type == "answer" and user_id != callee_id:
        return _error(user_id, call_id, conversation_id, "forbidden")

    return [
        CallDelivery(
            user_id=other_id,
            envelope=_envelope(
                call_id=call_id,
                conversation_id=conversation_id,
                signal_type=signal_type,
                sender_id=user_id,
                sender_name=sender_name,
                payload={str(key): value for key, value in payload.items()},
            ),
        )
    ]
