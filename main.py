import os
import logging
import asyncio
import sys
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from fastapi import FastAPI
import uvicorn
import threading
import time

# ---------------- Logging Setup ---------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# ---------------- Load Environment ---------------- #
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")

    if not API_ID or not API_HASH or not BOT_TOKEN:
        log.error("❌ Missing Environment Variables: API_ID/API_HASH/BOT_TOKEN required!")
        sys.exit(1)

    # Naya Variable yahan joda gaya hai
    BOT_USERNAME = os.getenv("BOT_USERNAME", "@Aapka_Bot_ka_Username") 
    
    CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0)) or None
    MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@examplechannel")
    CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Advanced Community")
    RULES_LINK = os.getenv("RULES_LINK", "https://t.me/example")
    SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/example")

    WEB_PORT = int(os.getenv("PORT", 8080))

except Exception as e:
    log.error(f"❌ Environment Variable Error: {e}")
    sys.exit(1)

# ---------------- Telegram Client ---------------- #
app = Client(
    "auto_approver_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    sleep_threshold=60 
)

TARGET_FILTER = filters.chat(CHANNEL_ID) if CHANNEL_ID else filters.all
CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"
# Bot ko group mein add karne ka link
ADD_TO_GROUP_LINK = f"https://t.me/{BOT_USERNAME.strip('@')}?startgroup=true"

# ---------------- FastAPI (Render Health Check) ---------------- #
web_app = FastAPI()

@web_app.get("/")
def home():
    return {"status": "✅ Bot is Running", "channel": CHANNEL_NAME}

# ---------------- Handlers (Start Message) ---------------- #
START_MESSAGE = (
    "👋 **Namaste {user_name}!** Main **{bot_name}** hoon.\n\n"
    "🤖 Mera kaam hai **{channel_name}** ke join requests ko turant approve karna!"
)

# START KEYBOARD mein "Add To Group" button joda gaya
START_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK),
        InlineKeyboardButton("➕ Group Mein Jorein", url=ADD_TO_GROUP_LINK)
    ],
    [
        InlineKeyboardButton("📚 Rules", url=RULES_LINK),
        InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
    ],
    [
        InlineKeyboardButton("👤 Status", callback_data="status_check")
    ]
])

@app.on_message(filters.command("start") & filters.private)
async def start_handler(_, message):
    try:
        bot_info = await app.get_me()
        await message.reply_text(
            START_MESSAGE.format(
                user_name=message.from_user.first_name,
                bot_name=bot_info.first_name,
                channel_name=CHANNEL_NAME
            ),
            reply_markup=START_KEYBOARD
        )
    except Exception as e:
        log.error(f"Error in /start: {e}")

@app.on_callback_query(filters.regex("status_check"))
async def status_checker(_, callback_query):
    await callback_query.answer("🚀 Bot is Active & Auto-approving!", show_alert=True)

# ---------------- 👑 AUTO APPROVAL HANDLER (CORRECTED FOR PM) 👑 ---------------- #

WELCOME_TEXT = (
    "⚜️ **APPROVED!** {user_name}, swagat hai aapka **{channel_name}** mein 🚀\n\n"
    "🎉 Aapka request **turant** accept ho gaya hai!\n"
    "👉 Latest updates aur features ke liye **{mandatory_channel}** join karein."
)

# WELCOME KEYBOARD mein "Add To Group" button joda gaya
WELCOME_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Narzox Channel Join Karein", url=CHANNEL_LINK),
        InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=ADD_TO_GROUP_LINK)
    ],
    [
        InlineKeyboardButton("📚 Rules", url=RULES_LINK),
        InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
    ]
])

@app.on_chat_join_request(TARGET_FILTER)
async def auto_approve(client: Client, req: ChatJoinRequest):
    user = req.from_user
    chat = req.chat

    log.info(f"--- Processing Join Request from {user.first_name} for {chat.title} ---")

    # 1. Request ko Approve karna
    try:
        await client.approve_chat_join_request(chat.id, user.id)
        log.info(f"✅ Approved: {user.first_name} for {chat.title}")
    except Exception as e:
        log.error(f"❌ Approval Failed: Check Bot Admin/Permission. Error: {e}")
        return

    # 2. Welcome Message PM (Private Message) mein bhejna
    try:
        await client.send_message(
            user.id, 
            WELCOME_TEXT.format(
                user_name=user.first_name,
                channel_name=chat.title,
                mandatory_channel=MANDATORY_CHANNEL
            ),
            reply_markup=WELCOME_KEYBOARD
        )
        log.info(f"✉️ Welcome PM sent to {user.first_name}")

    except FloodWait as e:
        log.warning(f"⏳ FloodWait on PM. Sleeping {e.value} sec.")
        time.sleep(e.value)
    except Exception as e:
        log.warning(f"⚠️ Failed to send PM to {user.first_name}. Error: {e}")


# ---------------- Runner ---------------- #
def run_fastapi():
    """Run FastAPI server (health check for Render)."""
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info")

if __name__ == "__main__":
    log.info("🚀 Starting Auto-Approve Bot (Hybrid Mode)...")

    # FastAPI ko alag thread mein shuru karna
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Pyrogram Bot ko main thread mein shuru karna
    app.run()
