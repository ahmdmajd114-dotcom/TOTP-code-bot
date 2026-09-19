"""Server-side Telegram scheduled messages sent from the owner's account.

The credentials are Render secrets only; no phone number, password, or login
code is stored in Supabase or the Google Sheet.
"""

from __future__ import annotations

import os
from datetime import datetime

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:  # Keeps local unit tests usable before Render installs deps.
    TelegramClient = None
    StringSession = None


def is_configured() -> bool:
    return bool(
        TelegramClient
        and os.environ.get("TELEGRAM_PERSONAL_API_ID")
        and os.environ.get("TELEGRAM_PERSONAL_API_HASH")
        and os.environ.get("TELEGRAM_PERSONAL_SESSION")
    )


def _client():
    if not is_configured():
        raise RuntimeError("Personal Telegram scheduler is not configured")
    return TelegramClient(
        StringSession(os.environ["TELEGRAM_PERSONAL_SESSION"]),
        int(os.environ["TELEGRAM_PERSONAL_API_ID"]),
        os.environ["TELEGRAM_PERSONAL_API_HASH"],
    )


async def schedule_message(chat_id: int, text: str, when: datetime) -> int:
    """Schedule a message in the owner's existing private chat with customer."""
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Personal Telegram session is no longer authorized")
        message = await client.send_message(chat_id, text, schedule=when)
        return int(message.id)
    finally:
        await client.disconnect()


async def send_message(chat_id: int, text: str) -> int:
    """يرسل رسالة فورية من حساب Telegram الشخصي، كبديل عن Business."""
    client = _client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram personal session is not authorized")
        message = await client.send_message(chat_id, text)
        return int(message.id)
    finally:
        await client.disconnect()


async def cancel_scheduled_message(chat_id: int, message_id: int) -> None:
    """Delete a previously scheduled message before Telegram sends it."""
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Personal Telegram session is no longer authorized")
        entity = await client.get_input_entity(chat_id)
        await client.delete_scheduled_messages(entity, [message_id])
    finally:
        await client.disconnect()
