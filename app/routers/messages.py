from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.messages import (
    add_group_member,
    create_group_conversation,
    get_linked_chats,
    get_messages_for_conversation,
    list_conversations,
    mark_conversation_as_read,
    mark_linked_chat_read,
    remove_group_member,
    rename_group_conversation,
    search_message_contacts,
    send_message,
    start_direct_conversation,
)

router = APIRouter(prefix="/messages", tags=["messages"])


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


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_id: UUID | None = None
    body: str
    created_at: object
    updated_at: object


class MessageResponse(BaseModel):
    message: MessageOut


class ConversationMessagesResponse(BaseModel):
    conversation_id: UUID
    total: int
    items: list[MessageOut]


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
    unread_count: int = 0


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
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return search_message_contacts(
        db=db,
        current_user_id=current_user_id,
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


@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
def send_conversation_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return send_message(
        db=db,
        current_user_id=current_user_id,
        conversation_id=conversation_id,
        body=payload.body,
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
