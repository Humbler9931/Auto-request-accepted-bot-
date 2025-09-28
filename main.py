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
    sleep_threshold=60  # Auto handle FloodWait
)

TARGET_FILTER = filters.chat(CHANNEL_ID) if CHANNEL_ID else filters.all
CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"

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

START_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📚 Rules", url=RULES_LINK),
     InlineKeyboardButton("📣 Main Channel Join Karein", url=CHANNEL_LINK)],
    [InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK),
     InlineKeyboardButton("👤 Status", callback_data="status_check")]
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

WELCOME_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Narzox Channel Join Karein", url=CHANNEL_LINK)],
    [InlineKeyboardButton("📚 Rules", url=RULES_LINK),
     InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)]
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
        return # Agar approve nahi hua to PM mat bhejo

    # 2. Welcome Message PM (Private Message) mein bhejna
    try:
        await client.send_message(
            # Yahan chat.id ki jagah user.id use kiya gaya hai (PM ke liye)
            user.id, 
            WELCOME_TEXT.format(
                user_name=user.first_name,
                channel_name=chat.title, # Channel ka naam use kiya
                mandatory_channel=MANDATORY_CHANNEL
            ),
            reply_markup=WELCOME_KEYBOARD
        )
        log.info(f"✉️ Welcome PM sent to {user.first_name}")

    except FloodWait as e:
        # FloodWait ko yahan handle kiya jaa raha hai
        log.warning(f"⏳ FloodWait on PM. Sleeping {e.value} sec.")
        time.sleep(e.value)
        # Message dobara bhejne ki koshish nahi karte, aage badhte hain
    except Exception as e:
        # PM fail ho sakta hai agar user ne bot ko kabhi start na kiya ho
        log.warning(f"⚠️ Failed to send PM to {user.first_name}. Error: {e}")


# ---------------- Runner ---------------- #
def run_fastapi():
    """Run FastAPI server (health check for Render)."""
    # Uvicorn.run blocking hai, yeh thread chalta rahega
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info")

if __name__ == "__main__":
    log.info("🚀 Starting Auto-Approve Bot (Hybrid Mode)...")

    # FastAPI ko alag thread mein shuru karna (Web Service/Port ke liye)
    # daemon=True se yeh thread main thread ke band hote hi band ho jayega
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Pyrogram Bot ko main thread mein shuru karna (Polling)
    # App.run() blocking hai, isliye yeh main process chalta rahega
    app.run()
