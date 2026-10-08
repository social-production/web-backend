from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException

from app.auth.cookies import ACCESS_COOKIE
from app.auth.dependencies import _is_blacklisted_jti, get_current_user_id
from app.auth.jwt import JWTError, get_access_token_payload
from app.cache import get_redis_client
from app.db import SessionLocal
from app.dependencies import get_db, get_messaging_provider
from app.ports import MessagingProvider
from app.services.messages import (
    add_group_member,
    create_group_conversation,
    delete_message,
    edit_message,
    get_linked_chats,
    get_messages_for_conversation,
    list_conversations,
    mark_conversation_as_read,
    mark_linked_chat_read,
    pin_message,
    read_message_attachment,
    remove_group_member,
    rename_group_conversation,
    send_message,
    set_conversation_preferences,
    set_linked_chat_preferences,
    start_direct_conversation,
    unpin_message,
)
from app.services.messages.calls import RedisCallStore, handle_call_signal

router = APIRouter(prefix="/messages", tags=["messages"])

MAX_UPLOAD_PART_BYTES = 12 * 1024 * 1024


class ParticipantOut(BaseModel):
    id: UUID
    username: str
    profileImageUrl: str | None = None


class ConversationOut(BaseModel):
    id: UUID
    kind: str
    title: str | None = None
    created_by: UUID | None = None
    created_at: object
    updated_at: object
    last_message_at: object
    preview: str = ""
    unread_count: int = 0
    list_pinned: bool = False
    muted: bool = False
    participants: list[ParticipantOut]


class ConversationResponse(BaseModel):
    conversation: ConversationOut


class StartDirectConversationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    other_username: str = Field(min_length=3, max_length=32)


class CreateGroupConversationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    participant_usernames: list[str] = Field(default_factory=list)


class RenameGroupConversationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)


class GroupMemberManageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=32)


class ConversationsListResponse(BaseModel):
    total: int
    items: list[ConversationOut]


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)
    reply_to_id: UUID | None = None


class EditMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)


class ChatListPreferencesRequest(BaseModel):
    pinned: bool | None = None
    muted: bool | None = None
    hidden: bool | None = None


class AttachmentOut(BaseModel):
    id: UUID
    kind: str
    filename: str
    content_type: str
    byte_size: int


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_id: UUID | None = None
    body: str
    created_at: object
    updated_at: object
    edited_at: object | None = None
    reply_to_id: UUID | None = None
    reply_author: str | None = None
    reply_preview: str | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)


class PinOut(BaseModel):
    message_id: UUID
    pinned_at: object
    preview: str = ""


class MessageResponse(BaseModel):
    message: MessageOut


class ConversationMessagesResponse(BaseModel):
    conversation_id: UUID
    total: int
    items: list[MessageOut]
    pins: list[PinOut] = Field(default_factory=list)
    can_pin: bool = False


class PinActionResponse(BaseModel):
    ok: bool


class ConversationReadResponse(BaseModel):
    ok: bool
    conversation_id: UUID
    last_read_at: object


class LinkedChatOut(BaseModel):
    id: str
    kind: str
    entity_id: str
    entity_slug: str
    title: str
    preview: str
    last_message_at: str
    comment_count: int
    member_count: int = 0
    members: list[str] = []
    unread_count: int = 0
    list_pinned: bool = False
    muted: bool = False


class LinkedChatsListResponse(BaseModel):
    total: int
    items: list[LinkedChatOut]


class MessageContactOut(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profileImageUrl: str | None = None


class MessageContactsResponse(BaseModel):
    total: int
    items: list[MessageContactOut]


@router.get("/contacts", response_model=MessageContactsResponse)
def list_message_contacts(
    q: str = Query(default="", max_length=32),
    limit: int = Query(default=8, ge=1, le=25),
    current_user_id: UUID = Depends(get_current_user_id),
    messaging: MessagingProvider = Depends(get_messaging_provider),
) -> dict[str, object]:
    return messaging.search_contacts(
        current_user_id,
        query=q,
        limit=limit,
    )


@router.post("/direct", response_model=ConversationResponse)
def start_direct(
    payload: StartDirectConversationRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return start_direct_conversation(
        db=db,
        current_user_id=current_user_id,
        other_username=payload.other_username,
    )


@router.post("/group", response_model=ConversationResponse)
def create_group(
    payload: CreateGroupConversationRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_group_conversation(
        db=db,
        current_user_id=current_user_id,
        title=payload.title,
        participant_usernames=payload.participant_usernames,
    )


@router.get("/conversations", response_model=ConversationsListResponse)
def list_my_conversations(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_conversations(db=db, current_user_id=current_user_id)


def _content_disposition(kind: str, filename: str) -> str:
    safe = filename.replace("\\", "_").replace('"', "").replace("\r", "").replace("\n", "")
    disposition = "inline" if kind == "image" else "attachment"
    return f'{disposition}; filename="{safe}"'


@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
async def send_conversation_message(
    conversation_id: UUID,
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    attachments: list = []
    reply_to_id: UUID | None = None
    if "multipart/form-data" in content_type:
        try:
            form = await request.form(max_part_size=MAX_UPLOAD_PART_BYTES)
        except MultiPartException as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Files must be 10MB or smaller",
            ) from exc
        raw_body = form.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        from app.services.messages.attachments import prepare_form_uploads

        attachments = await prepare_form_uploads(form)
        raw_reply = form.get("reply_to_id")
        if isinstance(raw_reply, str) and raw_reply.strip():
            try:
                reply_to_id = UUID(raw_reply.strip())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Reply target is invalid",
                ) from exc
    else:
        try:
            payload = SendMessageRequest.model_validate(await request.json())
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Message body is required",
            ) from exc
        body = payload.body
        reply_to_id = payload.reply_to_id

    return send_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        body=body,
        attachments=attachments,
        reply_to_id=reply_to_id,
    )


@router.patch(
    "/conversations/{conversation_id}/messages/{message_id}", response_model=MessageResponse
)
def edit_conversation_message(
    conversation_id: UUID,
    message_id: UUID,
    payload: EditMessageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return edit_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        body=payload.body,
    )


@router.delete("/conversations/{conversation_id}/messages/{message_id}", response_model=PinActionResponse)
def delete_conversation_message(
    conversation_id: UUID,
    message_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return delete_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )


@router.patch("/conversations/{conversation_id}/preferences", response_model=PinActionResponse)
def update_conversation_preferences(
    conversation_id: UUID,
    payload: ChatListPreferencesRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return set_conversation_preferences(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        pinned=payload.pinned,
        muted=payload.muted,
        hidden=payload.hidden,
    )


@router.get(
    "/conversations/{conversation_id}/messages", response_model=ConversationMessagesResponse
)
def get_conversation_messages(
    conversation_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_messages_for_conversation(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    payload = read_message_attachment(
        db=db, current_user_id=current_user_id, attachment_id=attachment_id
    )
    return Response(
        content=payload["data"],
        media_type=str(payload["content_type"]),
        headers={
            "Content-Disposition": _content_disposition(
                str(payload["kind"]), str(payload["filename"])
            ),
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/conversations/{conversation_id}/pins/{message_id}", response_model=PinActionResponse)
def pin_conversation_message(
    conversation_id: UUID,
    message_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return pin_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )


@router.delete(
    "/conversations/{conversation_id}/pins/{message_id}", response_model=PinActionResponse
)
def unpin_conversation_message(
    conversation_id: UUID,
    message_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return unpin_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )


@router.post("/conversations/{conversation_id}/read", response_model=ConversationReadResponse)
def mark_conversation_read_route(
    conversation_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return mark_conversation_as_read(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
def rename_group(
    conversation_id: UUID,
    payload: RenameGroupConversationRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return rename_group_conversation(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        title=payload.title,
    )


@router.post("/conversations/{conversation_id}/members", response_model=ConversationResponse)
def add_member_to_group(
    conversation_id: UUID,
    payload: GroupMemberManageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_group_member(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        username=payload.username,
    )


@router.delete(
    "/conversations/{conversation_id}/members/{username}", response_model=ConversationResponse
)
def remove_member_from_group(
    conversation_id: UUID,
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return remove_group_member(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        username=username,
    )


@router.get("/linked-chats", response_model=LinkedChatsListResponse)
def list_linked_chats(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_linked_chats(db=db, current_user_id=current_user_id)


@router.post("/linked-chats/{subject_type}/{subject_id}/read")
def mark_linked_chat_read_route(
    subject_type: str,
    subject_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return mark_linked_chat_read(
        db=db,
        current_user_id=current_user_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )


@router.patch("/linked-chats/{subject_type}/{subject_id}/preferences", response_model=PinActionResponse)
def update_linked_chat_preferences(
    subject_type: str,
    subject_id: UUID,
    payload: ChatListPreferencesRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return set_linked_chat_preferences(
        db=db,
        current_user_id=current_user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        pinned=payload.pinned,
        muted=payload.muted,
        hidden=payload.hidden,
    )


async def _websocket_user_id(websocket: WebSocket) -> UUID | None:
    token = websocket.cookies.get(ACCESS_COOKIE)
    if not token:
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        return None
    try:
        payload = get_access_token_payload(token)
    except JWTError:
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti or await _is_blacklisted_jti(jti):
        return None
    subject = payload.get("sub")
    if not isinstance(subject, str):
        return None
    try:
        return UUID(subject)
    except ValueError:
        return None


@router.websocket("/calls/ws")
async def call_signals(websocket: WebSocket) -> None:
    """Authenticated signal socket. Only direct-conversation members may signal."""
    user_id = await _websocket_user_id(websocket)
    await websocket.accept()
    if user_id is None:
        await websocket.close(code=4401)
        return

    store = RedisCallStore(get_redis_client())
    user_key = str(user_id)
    await store.join_presence(user_key)
    pubsub = get_redis_client().pubsub()
    await pubsub.subscribe(store.channel(user_key))

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, str):
                await websocket.send_text(data)

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(10)
            await store.refresh_presence(user_key)

    pump_task = asyncio.create_task(pump())
    keepalive_task = asyncio.create_task(keepalive())
    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or raw.get("type") == "ping":
                await store.refresh_presence(user_key)
                continue
            db = SessionLocal()
            try:
                deliveries = await handle_call_signal(db, store, user_id, raw)
            finally:
                db.close()
            for delivery in deliveries:
                await store.publish(str(delivery.user_id), delivery.envelope)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        keepalive_task.cancel()
        await asyncio.gather(pump_task, keepalive_task, return_exceptions=True)
        await pubsub.unsubscribe(store.channel(user_key))
        await pubsub.aclose()
        await store.leave_presence(user_key)
