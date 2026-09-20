"""Server-side Telegram scheduled messages sent from the owner's account.

The credentials are Render secrets only; no phone number, password, or login
code is stored in Supabase or the Google Sheet.
"""

from __future__ import annotations

import os
from datetime import datetime

try:
    from telethon import TelegramClient, Button
    from telethon.sessions import StringSession
except ImportError:  # Keeps local unit tests usable before Render installs deps.
    TelegramClient = None
    StringSession = None
    Button = None


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


async def _resolve_customer_entity(client, chat_id: int):
    """يستخرج access_hash للعميل من محادثات الحساب الشخصي عند الحاجة."""
    try:
        return await client.get_input_entity(chat_id)
    except (ValueError, TypeError):
        # الـchat_id وحده لا يكفي دائماً في MTProto، خصوصاً عندما تكون
        # الرسالة السابقة وصلت عن طريق Telegram Business لا Telethon.
        await client.get_dialogs(limit=None)
        return await client.get_input_entity(chat_id)


async def schedule_message(chat_id: int, text: str, when: datetime) -> int:
    """Schedule a message in the owner's existing private chat with customer."""
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Personal Telegram session is no longer authorized")
        entity = await _resolve_customer_entity(client, chat_id)
        message = await client.send_message(entity, text, schedule=when)
        return int(message.id)
    finally:
        await client.disconnect()


async def schedule_messages(
    messages: list[tuple[int, str, datetime]],
) -> dict[int, int | Exception]:
    """Reserve several messages with one Telegram session.

    Loading dialogs for every legacy customer can trigger Telegram flood waits.
    The startup backfill uses this function so it loads the owner's dialog list
    once, then resolves and schedules each customer independently.
    """
    client = _client()
    results: dict[int, int | Exception] = {}
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Personal Telegram session is no longer authorized")
        await client.get_dialogs(limit=None)
        for chat_id, text, when in messages:
            try:
                entity = await client.get_input_entity(chat_id)
                message = await client.send_message(entity, text, schedule=when)
                results[chat_id] = int(message.id)
            except Exception as exc:  # Continue with the rest of the customers.
                results[chat_id] = exc
    finally:
        await client.disconnect()
    return results


async def send_message(
    chat_id: int, text: str, *, button_text: str | None = None, button_url: str | None = None,
) -> int:
    """يرسل رسالة فورية من حساب Telegram الشخصي، كبديل عن Business."""
    client = _client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram personal session is not authorized")
        entity = await _resolve_customer_entity(client, chat_id)
        buttons = [[Button.url(button_text, button_url)]] if button_text and button_url and Button else None
        message = await client.send_message(entity, text, buttons=buttons)
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
