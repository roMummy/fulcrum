from __future__ import annotations

from threading import RLock


_lock = RLock()
_chatrooms_by_channel: dict[int, set[str]] = {}
_members_by_channel: dict[int, dict[str, set[str]]] = {}


def has_chatroom(channel_id: int, chatroom_id: str) -> bool:
    normalized_chatroom_id = str(chatroom_id or "").strip()
    if not normalized_chatroom_id:
        return False
    with _lock:
        return normalized_chatroom_id in _chatrooms_by_channel.get(int(channel_id), set())


def has_member(channel_id: int, chatroom_id: str, member_external_id: str) -> bool:
    normalized_chatroom_id = str(chatroom_id or "").strip()
    normalized_member_id = str(member_external_id or "").strip()
    if not normalized_chatroom_id or not normalized_member_id:
        return False
    with _lock:
        return normalized_member_id in _members_by_channel.get(int(channel_id), {}).get(normalized_chatroom_id, set())


def remember_chatroom(channel_id: int, chatroom_id: str) -> None:
    normalized_chatroom_id = str(chatroom_id or "").strip()
    if not normalized_chatroom_id:
        return
    with _lock:
        _chatrooms_by_channel.setdefault(int(channel_id), set()).add(normalized_chatroom_id)


def remember_member(channel_id: int, chatroom_id: str, member_external_id: str) -> None:
    normalized_chatroom_id = str(chatroom_id or "").strip()
    normalized_member_id = str(member_external_id or "").strip()
    if not normalized_chatroom_id or not normalized_member_id:
        return
    with _lock:
        _chatrooms_by_channel.setdefault(int(channel_id), set()).add(normalized_chatroom_id)
        _members_by_channel.setdefault(int(channel_id), {}).setdefault(normalized_chatroom_id, set()).add(normalized_member_id)


def remember_chatroom_members(channel_id: int, chatroom_id: str, member_external_ids: list[str]) -> None:
    normalized_chatroom_id = str(chatroom_id or "").strip()
    if not normalized_chatroom_id:
        return
    normalized_member_ids = {str(item or "").strip() for item in member_external_ids if str(item or "").strip()}
    with _lock:
        _chatrooms_by_channel.setdefault(int(channel_id), set()).add(normalized_chatroom_id)
        _members_by_channel.setdefault(int(channel_id), {})[normalized_chatroom_id] = normalized_member_ids
