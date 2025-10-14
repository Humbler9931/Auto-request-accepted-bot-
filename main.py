#!/usr/bin/env python3
import os
import logging
import asyncio
import sys
import time
import threading
from typing import Dict, Optional, Set, Tuple

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, Message, Chat
from pyrogram.errors import FloodWait, PeerIdInvalid, UserIsBlocked, UserNotParticipant, RPCError
from fastapi import FastAPI
import uvicorn

# ---------------- Logging Setup ---------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("UltraAutoApprover")

# ---------------- In-Memory Storage ---------------- #
USER_DATABASE: Set[int] = set()
PENDING_REQUESTS: Dict[Tuple[int, int], float] = {}  # (chat_id, user_id) -> timestamp

# ---------------- Load environment ---------------- #
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID", "0")) or None
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")

    # Optional / convenience
    # Channel ID to auto-approve (if provided). Agar ye set nahi kiya gaya to sabhi chats ke requests accept honge.
    CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
    AUTO_APPROVE_CHAT_ID: Optional[int] = int(CHANNEL_ID_ENV) if CHANNEL_ID_ENV and CHANNEL_ID_ENV.strip() else None

    # Optional developer id for broadcast (if not provided, broadcast disabled)
    DEVELOPER_ID_ENV = os.getenv("DEVELOPER_ID")
    DEVELOPER_ID: Optional[int] = int(DEVELOPER_ID_ENV) if DEVELOPER_ID_ENV and DEVELOPER_ID_ENV.strip() else None

    MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@your_channel")
    RULES_LINK = os.getenv("RULES_LINK", "https://t.me/example_rules")
    SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/example_support")

    WEB_PORT = int(os.getenv("PORT", 8080))

    if not API_ID or not API_HASH or not BOT_TOKEN:
        log.error("❌ Missing required env vars: API_ID / API_HASH / BOT_TOKEN are required.")
        sys.exit(1)

except Exception as e:
    log.error(f"❌ Error while reading environment variables: {e}")
    sys.exit(1)

CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"
# BOT_USERNAME will be set after client starts (fallback for links)
BOT_USERNAME: Optional[str] = None

# ---------------- Pyrogram Client ---------------- #
app = Client(
    "auto_approver_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    sleep_threshold=60,
    in_memory=True
)

# Variable to track if the cleaner task has started
app._cleaner_task_started = False

# ---------------- FastAPI Health-check ---------------- #
web_app = FastAPI()


@web_app.get("/")
def home():
    return {
        "status": "✅ Bot is running",
        "auto_approve_chat_id": AUTO_APPROVE_CHAT_ID or "ALL (All chats are supported)",
        "users_tracked": len(USER_DATABASE),
        "cleaner_task_active": app._cleaner_task_started
    }


# ---------------- Helper Functions ---------------- #
async def is_admin_or_creator(client: Client, chat_id: int, user_id: int) -> bool:
    """Checks if a user is an admin or creator in a chat."""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.debug(f"Could not check admin status for {user_id} in {chat_id}: {e}")
        return False


def build_start_keyboard(bot_username: Optional[str]) -> InlineKeyboardMarkup:
    """Builds the keyboard for the /start message."""
    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else "https://t.me/your_bot_here?startchannel=true"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📣 Support Channel", url=CHANNEL_LINK),
            InlineKeyboardButton("➕ADD ME ", url=add_group_link)
        ],
        [
            InlineKeyboardButton("📚 Rules", url=RULES_LINK),
            InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
        ],
        [
            InlineKeyboardButton("👤 Status & User Count", callback_data="status_check")
        ]
    ])


START_MESSAGE = (
    "👋 **Namaste {user_name}!**\n\n"
    "Main aapki madad karne wala bot hoon — chat join requests ko manage karta hoon aur "
    "agar permission mile to turant approve kar deta hoon. 🎯\n\n"
    "Use the buttons below to explore."
)

WELCOME_TEXT = (
    "⚜️ **APPROVED!** {user_name}, swagat hai aapka **{chat_title}** mein 🚀\n\n"
    "🎉 Aapka request **turant** accept ho gaya hai!\n"
    "👉 Updates ke liye {mandatory_channel} join karein."
)


def get_welcome_keyboard(chat: Chat, bot_username: Optional[str]) -> InlineKeyboardMarkup:
    """Builds the keyboard for the private welcome message."""
    # Prefer the configured mandatory channel link
    channel_btn = InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK)

    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else f"https://t.me/your_bot_here?startgroup=true"

    return InlineKeyboardMarkup([
        [channel_btn, InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=add_group_link)],
        [InlineKeyboardButton("📚 Rules", url=RULES_LINK), InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)]
    ])


# ---------------- Scheduled Background Task ---------------- #
async def pending_requests_cleaner(client: Client):
    """
    Background task to check and clear already pending requests periodically.
    Runs every 5 minutes (300 seconds).
    """
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            me = await client.get_me()
            BOT_USERNAME = me.username
            log.info(f"Bot Username set to: @{BOT_USERNAME}")
        except Exception:
            log.warning("Could not set BOT_USERNAME during cleaner startup.")

    while True:
        await asyncio.sleep(300)  # Wait for 5 minutes
        log.info("🧹 Starting scheduled check for already pending requests...")
        
        # 1. Determine which chats to check
        chats_to_check: Set[int] = set()
        if AUTO_APPROVE_CHAT_ID:
            # If a specific chat ID is configured, check only that one.
            chats_to_check.add(AUTO_APPROVE_CHAT_ID)
        else:
            # WARNING: Checking all dialogs can lead to FloodWait if the bot is in many chats.
            # We will use get_dialogs() but use a small limit and sleep to be safe.
            try:
                async for dialog in client.get_dialogs(limit=50):
                    # We only care about channels and supergroups
                    if dialog.chat.type in ["channel", "supergroup"]:
                        chats_to_check.add(dialog.chat.id)
                log.info(f"Found {len(chats_to_check)} chats/channels to check.")
            except FloodWait as fw:
                 log.warning(f"⚠️ FloodWait while getting dialogs: sleeping {fw.value}s")
                 await asyncio.sleep(fw.value)
            except Exception as e:
                log.error(f"Error getting dialogs for cleaner: {e}")

        # 2. Process pending requests for each chat
        for chat_id in chats_to_check:
            approved_count = 0
            
            try:
                # Get requests (limit 100 per check cycle per chat)
                async for req in client.get_chat_join_requests(chat_id, limit=100):
                    await client.approve_chat_join_request(chat_id, req.user.id)
                    USER_DATABASE.add(req.user.id) # Add to tracked users
                    approved_count += 1
                
                if approved_count > 0:
                    log.info(f"✅ Auto-cleaned {approved_count} pending requests in chat {chat_id}")
                    
            except RPCError as e:
                # This often happens if bot lost 'Manage Invite Links' permission
                log.warning(f"⚠️ RPCError while auto-cleaning {chat_id}: {e}")
            except Exception as e:
                log.error(f"❌ Unexpected error in auto-cleaner for {chat_id}: {e}")

        log.info("🧹 Scheduled pending request check finished.")


@app.on_message(filters.me)
async def startup_cleaner_scheduler(client: Client, message: Message):
    """
    This handler ensures the background cleaner task starts immediately
    after the bot successfully connects (using filters.me which fires on self-messages, 
    often a good proxy for bot readiness).
    """
    global BOT_USERNAME
    
    # Set BOT_USERNAME once
    if not BOT_USERNAME:
        try:
            me = await client.get_me()
            BOT_USERNAME = me.username
            log.info(f"Bot Username set to: @{BOT_USERNAME}")
        except Exception:
             pass # Will be handled by the cleaner task itself if needed

    # Start the background task only once
    if not client._cleaner_task_started:
        log.info("Starting background pending requests cleaner task...")
        # Start the task but don't wait for it
        asyncio.create_task(pending_requests_cleaner(client))
        client._cleaner_task_started = True
        log.info("Background pending cleaner task started successfully.")
    
    # Prevent the self-message from being processed further (optional)
    if message.command and message.command[0] == "start":
        return

# ---------------- Handlers ---------------- #
# (Remaining handlers are unchanged, except that the cleaner task is now running 
# in the background alongside them.)

# ... (start_handler, status_checker, auto_approve, manual_approve_handler, broadcast_handler are here)
# The existing handlers are omitted here for brevity but should remain in the actual file.

# ---------------- Manual approve command (admins only) ---------------- #
# ... (This section is unchanged and remains in the actual file)

# ---------------- Broadcast (developer only) ---------------- #
# ... (This section is unchanged and remains in the actual file)


# ---------------- Run ---------------- #
def run_fastapi():
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info")


if __name__ == "__main__":
    log.info("🚀 Starting Bot — FastAPI healthcheck + Pyrogram bot")

    # Start health check server in background thread
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Run pyrogram (blocks)
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("⌛ Shutting down (KeyboardInterrupt)")
    except Exception as e:
        log.error(f"🔥 Fatal error running pyrogram client: {e}")
