"""Backward-compatible re-exports for messaging services."""

from __future__ import annotations

from app.services.messages.attachments import (
    pin_message,
    prepare_attachment,
    read_message_attachment,
    unpin_message,
)
from app.services.messages.contacts import search_message_contacts
from app.services.messages.conversations import (
    add_group_member,
    create_group_conversation,
    find_direct_conversation_between,
    list_conversations,
    remove_group_member,
    rename_group_conversation,
    set_conversation_preferences,
    start_direct_conversation,
)
from app.services.messages.linked_chats import (
    get_linked_chats,
    mark_linked_chat_read,
    set_linked_chat_preferences,
)
from app.services.messages.messaging import (
    delete_message,
    edit_message,
    get_messages_for_conversation,
    get_total_unread_message_count,
    mark_conversation_as_read,
    send_message,
)

__all__ = [
    "add_group_member",
    "create_group_conversation",
    "find_direct_conversation_between",
    "get_linked_chats",
    "get_messages_for_conversation",
    "get_total_unread_message_count",
    "list_conversations",
    "mark_conversation_as_read",
    "delete_message",
    "edit_message",
    "mark_linked_chat_read",
    "pin_message",
    "prepare_attachment",
    "read_message_attachment",
    "remove_group_member",
    "rename_group_conversation",
    "search_message_contacts",
    "send_message",
    "set_conversation_preferences",
    "set_linked_chat_preferences",
    "start_direct_conversation",
    "unpin_message",
]
