"""Create a Telegram StringSession locally for Render.

Run this only in a terminal you control. It never writes your phone number,
login code, password, or generated session string to disk.
"""

import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("Telegram API ID: ").strip())
    api_hash = input("Telegram API hash: ").strip()
    phone = input("Phone number (+countrycode...): ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("Telegram login code (type locally, do not share): ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass("Two-step password (if enabled): "))
        print("\nCopy this entire session into Render as TELEGRAM_PERSONAL_SESSION:")
        print(client.session.save())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
