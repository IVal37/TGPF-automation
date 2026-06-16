import asyncio
from pyrogram import Client

app = Client(
    "my_account",
    api_id=38023056,
    api_hash="e42f8c286585e81dbf8f4dda0f39afb9"
)

async def main():
    async with app:

        await app.send_message(chat_id="Test Channel", text="testing")

if __name__ == "__main__":
    asyncio.run(main())