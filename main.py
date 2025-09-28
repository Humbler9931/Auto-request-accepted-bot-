import os
import logging
import asyncio
import sys # Naya import
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import FloodWait # Naya import
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI
import uvicorn
from uvicorn.config import Config

# --- Configuration & Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

load_dotenv()

# --- Environment Variables Fetching aur Validation (FloodWait ke liye zaroori) ---
try:
    # API_ID ko integer mein convert karte samay error check karna
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN") 
    
    if not API_ID or not API_HASH or not BOT_TOKEN:
        log.error("❌ Zaroori Environment Variables missing. Check .env/Render settings.")
        sys.exit(1)

    CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
    MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@narzoxbot")
    CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Advanced Community")
    RULES_LINK = os.getenv("RULES_LINK", "https://t.me/narzoxbot")
    SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/narzoxbot")

    # Render Web Server Configuration
    WEB_HOST = os.getenv("HOST", "0.0.0.0")
    WEB_PORT = int(os.getenv("PORT", 8080)) 

    # ID ko integer mein convert karna
    CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR else None

except ValueError:
    log.error("❌ API_ID or CHANNEL_ID is not a valid integer. Deployment stopped.")
    sys.exit(1)
except Exception as e:
    log.error(f"❌ Critical Environment Error: {e}")
    sys.exit(1)


# Filters
TARGET_FILTER = filters.chat(CHANNEL_ID) if CHANNEL_ID else filters.all
CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"

# --- 🎯 Pyrogram Client Initialization (FloodWait Fix) ---
try:
    # FloodWait ko automatic handle karne ke liye 'sleep_threshold' set kiya gaya hai.
    app = Client(
        "auto_approver_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        sleep_threshold=60  # Bot 60 seconds tak ki FloodWait ko khud hi handle karega
    )
    log.info("✅ Pyrogram Client object initialized.")
except Exception as e:
    log.error(f"❌ Pyrogram Initialization Failed: {e}")
    sys.exit(1)

# --- FastAPI App Initialization (Render Health Check) ---
web_app = FastAPI()

@web_app.get("/", tags=["Health Check"])
def home():
    """Render ko '200 OK' response deta hai taaki woh jaane ki service chal rahi hai."""
    return {"status": "Bot is operational (Hybrid Mode Active)."}

# --- HANDLERS (Same as before) ---

# START MESSAGE DEFINITIONS... (Handlers ka code yahan aayega)
START_MESSAGE = (
    "👋 **Namaste {user_name}!** Main **{bot_name}** hoon.\n\n"
    "🤖 **Mera Kaam:** Main aapki community **{channel_name}** ka *Gatekeeper* hoon. "
    "Mera mukhya (primary) kaam **Channel Join Requests** ko turant (instantly) **Approve** karna hai.\n"
)

START_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📚 Rules aur Jaankari", url=RULES_LINK),
        InlineKeyboardButton("📣 Main Channel Join Karein", url=CHANNEL_LINK)
    ],
    [
        InlineKeyboardButton("🛠️ Support/Help", url=SUPPORT_LINK),
        InlineKeyboardButton("👤 Mera Status Jaanein", callback_data="status_check")
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
            reply_markup=START_KEYBOARD,
            parse_mode="markdown"
        )
        log.info(f"Start command received from user: {message.from_user.id}")
    except Exception as e:
        log.error(f"Error in start_handler: {e}")

@app.on_callback_query(filters.regex("status_check"))
async def status_checker(_, callback_query):
    await callback_query.answer("🚀 Bot is Active and Serving! Auto-approval system chal raha hai.", show_alert=True)

# AUTO APPROVAL HANDLER
WELCOME_TO_CHANNEL_TEXT = (
    "**⚜️ APPROVED!** {user_name}, aapka swagat hai **{channel_name}** mein! 🚀\n\n"
    "Aapka request turant **Auto-Approved** ho gaya hai.\n"
    "Saare tools aur updates ke liye kripya **{mandatory_channel}** channel ko check karein aur usse Jude rahen. Dhanyavaad!"
)

WELCOME_TO_CHANNEL_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Narzox Channel/Bot 🌐", url=CHANNEL_LINK),
    ],
    [
        InlineKeyboardButton("📚 Rules", url=RULES_LINK),
        InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
    ]
])

@app.on_chat_join_request(TARGET_FILTER)
async def handle_join_request(client: Client, update: ChatJoinRequest):
    user = update.from_user
    chat = update.chat
    
    log.info(f"--- NEW JOIN REQUEST: {user.first_name} for {chat.title} ---")
    
    # 1. Request ko Approve karna
    try:
        await client.approve_chat_join_request(
            chat_id=chat.id, 
            user_id=user.id
        )
        log.info(f"✅ SUCCESSFULLY APPROVED: {user.first_name}")

    except Exception as e:
        # Permission error aane par yahan ruk jayega
        log.error(f"❌ APPROVAL FAILED: Check Bot Permissions (Add Members) in {chat.title}. Error: {e}")
        return 
        
    # 2. Channel mein Stylish Welcome Message Bhejna
    try:
        await client.send_message(
            chat_id=chat.id, 
            text=WELCOME_TO_CHANNEL_TEXT.format(
                user_name=user.first_name,
                channel_name=CHANNEL_NAME,
                mandatory_channel=MANDATORY_CHANNEL
            ),
            reply_markup=WELCOME_TO_CHANNEL_KEYBOARD,
            parse_mode="markdown"
        )
        log.info("✉️ Channel welcome message sent.")

    except Exception as e:
        log.warning(f"⚠️ Channel Welcome Message FAILED (Missing 'Post Messages' permission?). Error: {e}")
        
    
    # 3. User ko Personal Message (PM) Bhejna
    try:
        pm_message = f"**Welcome!** 🎉 {user.first_name}, aapki entry **{chat.title}** mein safal rahi. Ab aap community ka hissa hain. Enjoy!"
        
        await client.send_message(
            chat_id=user.id, 
            text=pm_message,
            parse_mode="markdown"
        )
        log.info(f"✉️ PM sent to user: {user.first_name}")

    except Exception:
        log.warning(f"⚠️ PM FAILED for {user.first_name} (Not started bot or privacy settings).")

# --- Main Execution Block for Hybrid Mode (is_running Fix) ---

async def run_bot_and_server():
    """Pyrogram client aur Uvicorn server ko ek hi loop mein chalaana."""
    
    log.info("🚀 Pyrogram Client starting...")
    # Pyrogram client ko shuru karna
    await app.start()

    log.info("🌟 Pyrogram Client Started Successfully!")

    # Uvicorn Web Server ki configuration
    server_config = Config(
        web_app, 
        host=WEB_HOST, 
        port=WEB_PORT, 
        log_level="info", 
        loop="asyncio"
    )
    server = uvicorn.Server(server_config)
    
    log.info(f"🌐 Starting Uvicorn Web Server on {WEB_HOST}:{WEB_PORT} (Render Health Check)")
    
    # Uvicorn server ko shuru karna (Blocking call)
    await server.serve()


if __name__ == "__main__":
    try:
        log.info("--- STARTING HYBRID BOT SERVICE ---")
        # asyncio.run(run_bot_and_server()) yeh loop conflict se bachata hai.
        asyncio.run(run_bot_and_server())
    except KeyboardInterrupt:
        log.info("Bot Stopped by User.")
    except Exception as e:
        log.error(f"A FATAL ERROR occurred: {e}")
        # Final cleanup: Error aane par client ko sahi tarah se stop karna
        if app.is_running:
            log.info("Stopping Pyrogram client on error...")
            asyncio.run(app.stop()) 
