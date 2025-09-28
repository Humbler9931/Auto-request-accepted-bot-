import os
import logging
import asyncio
import sys
import time
from typing import Dict, Any, Optional, Set

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, Message, Chat
from pyrogram.errors import FloodWait, UserNotParticipant, PeerIdInvalid, RPCError, UserIsBlocked, RPCError
from pyrogram.enums import ChatType
from fastapi import FastAPI
import uvicorn
import threading

# ---------------- Logging Setup ---------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# --- In-Memory Storage for Features --- #
# Database Simulation: Stores user IDs for broadcasting
USER_DATABASE: Set[int] = set()
# Join Request Tracker: Key: (chat_id, user_id) -> Value: timestamp
PENDING_REQUESTS: Dict[tuple, float] = {}

# ---------------- Load Environment & Config ---------------- #
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    
    # --- New Mandatory Configuration for Broadcast Feature ---
    DEVELOPER_ID = int(os.getenv("DEVELOPER_ID")) 
    
    if not API_ID or not API_HASH or not BOT_TOKEN or not DEVELOPER_ID:
        log.error("❌ Missing Environment Variables: API_ID/API_HASH/BOT_TOKEN/DEVELOPER_ID required!")
        sys.exit(1)

    # General Configuration
    BOT_USERNAME = os.getenv("BOT_USERNAME", "YOUR_BOT_USERNAME") 
    MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@pyrogram_community")
    CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Advanced Community")
    RULES_LINK = os.getenv("RULES_LINK", "https://t.me/example_rules")
    SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/example_support")
    
    # Optional Specific Chat ID for Auto-Approval
    AUTO_APPROVE_CHAT_ID: Optional[int] = int(os.getenv("AUTO_APPROVE_CHAT_ID", 0)) or None

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
    sleep_threshold=60,
    in_memory=True
)

# Filters
TARGET_FILTER = filters.chat(AUTO_APPROVE_CHAT_ID) if AUTO_APPROVE_CHAT_ID else filters.chat_join_request
CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"
ADD_TO_GROUP_LINK = f"https://t.me/{BOT_USERNAME.strip('@')}?startgroup=true"

# ---------------- FastAPI (Render Health Check) ---------------- #
web_app = FastAPI()

@web_app.get("/")
def home():
    """Health check endpoint."""
    return {
        "status": "✅ Advanced Bot is Running", 
        "target_chat_id": AUTO_APPROVE_CHAT_ID or "ALL",
        "users_in_db": len(USER_DATABASE)
    }

# ---------------- Helper Functions ---------------- #

async def is_admin_or_creator(client: Client, chat_id: int, user_id: int) -> bool:
    """Checks if a user is an admin or creator of the specified chat."""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.error(f"Error checking admin status: {e}")
        return False

# ---------------- Handlers (Start Message & Status) ---------------- #
START_MESSAGE = (
    "👋 **Namaste {user_name}!** Main **{bot_name}** hoon.\n\n"
    "🤖 Mera kaam hai **chat join requests** ko manage karna aur turant approve karna."
    "Aur ab mere paas **Broadcasting** ki shakti bhi hai! ⚡️"
)

START_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📣 Support Channel", url=CHANNEL_LINK),
        InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=ADD_TO_GROUP_LINK)
    ],
    [
        InlineKeyboardButton("📚 Rules", url=RULES_LINK),
        InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
    ],
    [
        InlineKeyboardButton("👤 Status & User Count", callback_data="status_check")
    ]
])

@app.on_message(filters.command("start") & filters.private)
async def start_handler(_, message: Message):
    """Handles the /start command in private chat and adds user to DB."""
    
    # 🌟 NEW: Add user to the in-memory database
    user_id = message.from_user.id
    if user_id not in USER_DATABASE:
        USER_DATABASE.add(user_id)
        log.info(f"🆕 User {user_id} added to database for broadcast.")
        
    try:
        bot_info = await app.get_me()
        await message.reply_text(
            START_MESSAGE.format(
                user_name=message.from_user.first_name,
                bot_name=bot_info.first_name,
            ),
            reply_markup=START_KEYBOARD
        )
    except Exception as e:
        log.error(f"Error in /start: {e}")

@app.on_callback_query(filters.regex("status_check"))
async def status_checker(_, callback_query):
    """Handles the status check inline button."""
    user_count = len(USER_DATABASE)
    await callback_query.answer(
        f"🚀 Bot Active | Total Users: {user_count} | Auto-approving (if configured)!", 
        show_alert=True
    )

# ---------------- 👑 AUTO APPROVAL HANDLER 👑 ---------------- #
WELCOME_TEXT = (
    "⚜️ **APPROVED!** {user_name}, swagat hai aapka **{chat_title}** mein 🚀\n\n"
    "🎉 Aapka request **turant** accept ho gaya hai!\n"
    "👉 Latest updates aur features ke liye **{mandatory_channel}** join karein."
)

def get_welcome_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    """Generates the dynamic welcome keyboard."""
    
    if chat.username and f"@{chat.username}" == MANDATORY_CHANNEL:
        channel_button = InlineKeyboardButton("✅ Channel Mein Jaayein", url=chat.invite_link or f"https://t.me/{chat.username}")
    else:
        channel_button = InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK)

    return InlineKeyboardMarkup([
        [
            channel_button,
            InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=ADD_TO_GROUP_LINK)
        ],
        [
            InlineKeyboardButton("📚 Rules", url=RULES_LINK),
            InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)
        ]
    ])

@app.on_chat_join_request(TARGET_FILTER)
async def auto_approve(client: Client, req: ChatJoinRequest):
    """Automatically approves join requests and sends welcome PM."""
    user = req.from_user
    chat = req.chat

    log.info(f"--- Processing Join Request from {user.first_name} ({user.id}) for {chat.title} ({chat.id}) ---")
    
    request_key = (chat.id, user.id)
    PENDING_REQUESTS[request_key] = time.time()
    
    # 🌟 NEW: Add user to the in-memory database upon successful approval
    if user.id not in USER_DATABASE:
        USER_DATABASE.add(user.id)
        log.info(f"🆕 User {user.id} added to database from join request.")

    # 1. Request ko Approve karna
    try:
        await client.approve_chat_join_request(chat.id, user.id)
        log.info(f"✅ Approved: {user.first_name} for {chat.title}")
        PENDING_REQUESTS.pop(request_key, None)
        
    except RPCError as e:
        log.error(f"❌ Approval Failed (RPC Error - Check Bot Admin/Permission for {chat.title}): {e}")
        return
    except Exception as e:
        log.error(f"❌ Approval Failed (General Error): {e}")
        return

    # 2. Welcome Message PM (Private Message) mein bhejna
    try:
        await client.send_message(
            user.id, 
            WELCOME_TEXT.format(
                user_name=user.first_name,
                chat_title=chat.title,
                mandatory_channel=MANDATORY_CHANNEL
            ),
            reply_markup=get_welcome_keyboard(chat)
        )
        log.info(f"✉️ Welcome PM sent to {user.first_name}")

    except FloodWait as e:
        log.warning(f"⏳ FloodWait on PM. Sleeping {e.value} sec.")
        await asyncio.sleep(e.value)
        # Attempt to send again after sleep
        try:
             await client.send_message(
                user.id, 
                WELCOME_TEXT.format(
                    user_name=user.first_name,
                    chat_title=chat.title,
                    mandatory_channel=MANDATORY_CHANNEL
                ),
                reply_markup=get_welcome_keyboard(chat)
            )
        except Exception as retry_e:
            log.warning(f"⚠️ Retry Failed to send PM to {user.first_name}. Error: {retry_e}")
            
    except PeerIdInvalid:
        log.warning(f"⚠️ Failed to send PM to {user.first_name}. User blocked bot.")
    except Exception as e:
        log.warning(f"⚠️ Failed to send PM to {user.first_name}. Error: {e}")


# ---------------- Advanced Admin Commands (Manual Approval) ---------------- #

@app.on_message(filters.command("approve") & filters.group)
async def manual_approve_handler(client: Client, message: Message):
    """Handles manual approval command by group admins."""
    
    # 1. Admin Check
    if not await is_admin_or_creator(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Kshama**! Yeh command sirf **Admins** ke liye hai.")
        return

    # [Remaining approval logic is the same as the previous version...]
    # 2. Target User Identification
    target_user_id = None
    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1 and message.command[1].isdigit():
        target_user_id = int(message.command[1])
    else:
        await message.reply_text("❓ **Kripya** uss user ko reply karein ya uska user ID `/approve <user_id>` format mein dein jise aap approve karna chahte hain.")
        return

    # 3. Manual Approval & PM Logic (Re-used for brevity)
    request_key = (message.chat.id, target_user_id)
    try:
        await client.approve_chat_join_request(message.chat.id, target_user_id)
        PENDING_REQUESTS.pop(request_key, None)
        
        approved_user = await client.get_users(target_user_id)
        
        confirmation_text = f"✅ **Manually Approved!** **{approved_user.first_name}** ({approved_user.id}) ko **{message.chat.title}** mein jodne ki anumati de di gayi hai."
        await message.reply_text(confirmation_text)
        
        # Add to DB on manual approval too
        if target_user_id not in USER_DATABASE:
            USER_DATABASE.add(target_user_id)
            
        # Optional: Send PM (re-using the logic from auto_approve)
        try:
            await client.send_message(
                target_user_id, 
                WELCOME_TEXT.format(
                    user_name=approved_user.first_name,
                    chat_title=message.chat.title,
                    mandatory_channel=MANDATORY_CHANNEL
                ),
                reply_markup=get_welcome_keyboard(message.chat)
            )
        except Exception as pm_e:
            log.warning(f"⚠️ Failed to send PM after manual approval to {approved_user.first_name}. Error: {pm_e}")
            
    except RPCError as e:
        await message.reply_text(f"❌ **Approval Failed!** ({e.__class__.__name__}): Bot ke paas Admin Permissions nahi hain ya request expire ho chuka hai.")
    except Exception as e:
        await message.reply_text(f"❌ **Approval Failed!** (General Error): {e}")

# ---------------- ⚡️ BROADCAST HANDLER ⚡️ ---------------- #

@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client: Client, message: Message):
    """Sends a message to all users in the USER_DATABASE."""
    
    # 1. Developer/Owner Check
    if message.from_user.id != DEVELOPER_ID:
        await message.reply_text("⛔ **Anumati nahi**! Yeh command sirf bot ke **Developer** ke liye hai.")
        return

    # 2. Get the content to broadcast
    if not message.reply_to_message:
        await message.reply_text("❓ **Kripya** uss message ko reply karein jise aap **broadcast** karna chahte hain.")
        return

    # 3. Preparation & Execution
    log.info(f"✨ Starting broadcast to {len(USER_DATABASE)} users...")
    
    sent_count = 0
    blocked_count = 0
    
    broadcast_message = message.reply_to_message
    
    await message.reply_text(f"🚀 **Broadcast Shuru**! Message ko **{len(USER_DATABASE)}** users tak bheja ja raha hai...")

    for user_id in list(USER_DATABASE): # Use list() to avoid issues if set changes during iteration
        try:
            await broadcast_message.copy(user_id)
            sent_count += 1
            await asyncio.sleep(0.05) # Small delay to respect rate limits
            
        except FloodWait as e:
            log.warning(f"⏳ FloodWait during broadcast. Sleeping {e.value} sec.")
            await asyncio.sleep(e.value)
            # Retry after sleep
            try:
                await broadcast_message.copy(user_id)
                sent_count += 1
            except Exception:
                pass # Ignore if retry also fails
                
        except (UserIsBlocked, UserNotParticipant, PeerIdInvalid, RPCError):
            # User blocked the bot or is no longer reachable
            USER_DATABASE.discard(user_id) 
            blocked_count += 1
            
        except Exception as e:
            log.error(f"❌ Error sending broadcast to {user_id}: {e}")
        
    # 4. Final Report
    final_report = (
        "✅ **Broadcast Samapt!**\n"
        f"➡️ **Sent Successfully**: {sent_count} messages\n"
        f"🚫 **Blocked/Failed**: {blocked_count} users\n"
        f"👥 **Current User Count**: {len(USER_DATABASE)}"
    )
    
    await message.reply_text(final_report)
    log.info(f"✨ Broadcast Finished. Report: Sent={sent_count}, Blocked={blocked_count}")


# ---------------- Runner ---------------- #
def run_fastapi():
    """Run FastAPI server (health check)."""
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="error")

if __name__ == "__main__":
    log.info("🚀 Starting Ultra Advanced Bot (Hybrid Mode with Broadcast)...")

    # FastAPI ko alag thread mein shuru karna (For health check)
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Pyrogram Bot ko main thread mein shuru karna
    app.run()
