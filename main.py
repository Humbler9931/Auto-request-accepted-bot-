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
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton, Message, Chat, ChatMemberUpdated
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
PROCESSED_CHATS: Set[int] = set()  # Track which chats have been initially processed

# ---------------- Load environment ---------------- #
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID", "0")) or None
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")

    # Optional / convenience
    CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
    AUTO_APPROVE_CHAT_ID: Optional[int] = int(CHANNEL_ID_ENV) if CHANNEL_ID_ENV and CHANNEL_ID_ENV.strip() else None

    DEVELOPER_ID_ENV = os.getenv("DEVELOPER_ID")
    DEVELOPER_ID: Optional[int] = int(DEVELOPER_ID_ENV) if DEVELOPER_ID_ENV and DEVELOPER_ID_ENV.strip() else None

    MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@narzoxbot")
    RULES_LINK = os.getenv("RULES_LINK", "https://t.me/teamrajweb")
    SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/narzoxbot")

    WEB_PORT = int(os.getenv("PORT", 8080))

    if not API_ID or not API_HASH or not BOT_TOKEN:
        log.error("❌ Missing required env vars: API_ID / API_HASH / BOT_TOKEN are required.")
        sys.exit(1)

except Exception as e:
    log.error(f"❌ Error while reading environment variables: {e}")
    sys.exit(1)

CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"
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

app._cleaner_task_started = False

# ---------------- FastAPI Health-check ---------------- #
web_app = FastAPI()


@web_app.get("/")
def home():
    """Health check for external pinger services (like UptimeRobot)."""
    return {
        "status": "✅ Bot is Running (via FastAPI)",
        "auto_approve_chat_id": AUTO_APPROVE_CHAT_ID or "ALL (Using Safe Dialog Check)",
        "users_tracked": len(USER_DATABASE),
        "cleaner_task_active": app._cleaner_task_started,
        "processed_chats": len(PROCESSED_CHATS)
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
    channel_btn = InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK)
    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else f"https://t.me/your_bot_here?startgroup=true"

    return InlineKeyboardMarkup([
        [channel_btn, InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=add_group_link)],
        [InlineKeyboardButton("📚 Rules", url=RULES_LINK), InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)]
    ])


# ---------------- Core: Send Welcome DM ---------------- #
async def send_welcome_dm(client: Client, user_id: int, user_name: str, chat_title: str):
    """Send personalized welcome DM to approved user."""
    global BOT_USERNAME
    
    try:
        if not BOT_USERNAME:
            me = await client.get_me()
            BOT_USERNAME = me.username
        
        chat_obj = type('obj', (object,), {'title': chat_title})()
        
        await client.send_message(
            user_id,
            WELCOME_TEXT.format(
                user_name=user_name,
                chat_title=chat_title,
                mandatory_channel=MANDATORY_CHANNEL
            ),
            reply_markup=get_welcome_keyboard(chat_obj, BOT_USERNAME)
        )
        log.info(f"✉️ Welcome DM sent to {user_id} ({user_name})")
        return True
        
    except (PeerIdInvalid, UserIsBlocked, UserNotParticipant) as e:
        log.debug(f"⚠️ Cannot send DM to {user_id}: {type(e).__name__}")
        return False
    except Exception as e:
        log.warning(f"⚠️ Failed to send DM to {user_id}: {e}")
        return False


# ---------------- Core: Approve Single Request ---------------- #
async def approve_request_with_dm(client: Client, chat_id: int, user_id: int, user_name: str, chat_title: str):
    """Approve a single request and send DM."""
    try:
        await client.approve_chat_join_request(chat_id, user_id)
        USER_DATABASE.add(user_id)
        log.info(f"✅ Approved: {user_name} ({user_id}) -> {chat_title} ({chat_id})")
        
        # Send welcome DM
        await send_welcome_dm(client, user_id, user_name, chat_title)
        
        return True
        
    except FloodWait as fw:
        log.warning(f"⏳ FloodWait: sleeping {fw.value}s")
        await asyncio.sleep(fw.value)
        return await approve_request_with_dm(client, chat_id, user_id, user_name, chat_title)
        
    except RPCError as e:
        log.error(f"❌ RPCError approving {user_id} in {chat_id}: {e}")
        return False
        
    except Exception as e:
        log.error(f"❌ Error approving {user_id}: {e}")
        return False


# ---------------- Core: Clear All Pending Requests for a Chat ---------------- #
async def clear_pending_requests(client: Client, chat_id: int, chat_title: str = None):
    """Clear all pending requests for a specific chat."""
    if chat_id in PROCESSED_CHATS:
        log.debug(f"Chat {chat_id} already processed, skipping.")
        return 0
    
    approved_count = 0
    
    try:
        # Get chat details if title not provided
        if not chat_title:
            try:
                chat = await client.get_chat(chat_id)
                chat_title = chat.title or f"Chat {chat_id}"
            except:
                chat_title = f"Chat {chat_id}"
        
        log.info(f"🧹 Starting to clear pending requests for: {chat_title} ({chat_id})")
        
        # Get all pending requests (limit 200 per cycle to avoid timeout)
        async for req in client.get_chat_join_requests(chat_id, limit=200):
            user = req.from_user
            user_name = user.first_name or "User"
            
            success = await approve_request_with_dm(
                client, 
                chat_id, 
                user.id, 
                user_name, 
                chat_title
            )
            
            if success:
                approved_count += 1
            
            # Small delay to avoid flooding
            await asyncio.sleep(0.1)
        
        if approved_count > 0:
            log.info(f"🎉 Cleared {approved_count} pending requests from {chat_title}")
        else:
            log.info(f"✓ No pending requests in {chat_title}")
        
        PROCESSED_CHATS.add(chat_id)
        return approved_count
        
    except FloodWait as fw:
        log.warning(f"⏳ FloodWait during clearing {chat_id}: sleeping {fw.value}s")
        await asyncio.sleep(fw.value)
        return await clear_pending_requests(client, chat_id, chat_title)
        
    except RPCError as e:
        log.error(f"❌ RPCError clearing {chat_id}: {e} (Check bot permissions)")
        return approved_count
        
    except Exception as e:
        log.error(f"❌ Error clearing pending requests for {chat_id}: {e}")
        return approved_count


# ---------------- Scheduled Background Cleaner Task ---------------- #
async def pending_requests_cleaner(client: Client):
    """
    Background task to check and clear pending requests periodically (every 5 mins).
    """
    global BOT_USERNAME
    
    # Initial delay before first run
    await asyncio.sleep(10)
    
    while True:
        log.info("🧹 Starting scheduled check for pending requests...")
        
        chats_to_check: Set[int] = set()
        
        if AUTO_APPROVE_CHAT_ID:
            # Check only the configured chat
            chats_to_check.add(AUTO_APPROVE_CHAT_ID)
            log.debug(f"Checking only the configured chat: {AUTO_APPROVE_CHAT_ID}")
        else:
            # Check recent 100 chats (optimized)
            try:
                async for dialog in client.get_dialogs(limit=100):
                    if dialog.chat.type in ["channel", "supergroup"]:
                        chats_to_check.add(dialog.chat.id)
                log.info(f"Found {len(chats_to_check)} chats/channels to check.")
            except Exception as e:
                log.error(f"Error getting dialogs for cleaner: {e}")

        # Process pending requests for each chat
        total_approved = 0
        for chat_id in chats_to_check:
            # Skip already processed chats in this cycle
            if chat_id in PROCESSED_CHATS:
                continue
                
            approved = await clear_pending_requests(client, chat_id)
            total_approved += approved

        if total_approved > 0:
            log.info(f"🎉 Scheduled check finished. Total approved: {total_approved}")
        else:
            log.info("🧹 Scheduled check finished. No pending requests found.")
        
        # Clear processed chats set for next cycle
        PROCESSED_CHATS.clear()
        
        # Wait 5 minutes before next cycle
        await asyncio.sleep(300)


# ---------------- NEW: Bot Added to Channel Handler ---------------- #
@app.on_chat_member_updated()
async def bot_added_to_chat(client: Client, update: ChatMemberUpdated):
    """
    When bot is added to a new chat/channel, immediately clear all pending requests.
    """
    # Check if this update is about our bot
    me = await client.get_me()
    
    if update.new_chat_member and update.new_chat_member.user.id == me.id:
        # Bot was just added
        if update.new_chat_member.status in ["administrator", "member"]:
            chat = update.chat
            log.info(f"🆕 Bot added to new chat: {chat.title} ({chat.id})")
            
            # Wait a moment for permissions to propagate
            await asyncio.sleep(2)
            
            # Clear all pending requests
            approved = await clear_pending_requests(client, chat.id, chat.title)
            
            if approved > 0:
                log.info(f"🎊 Auto-cleared {approved} pending requests after being added to {chat.title}")


# ---------------- Handlers ---------------- #
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    USER_DATABASE.add(user.id)
    log.info(f"🆕 /start from {user.id} — added to USER_DATABASE (count={len(USER_DATABASE)})")

    try:
        global BOT_USERNAME
        if not BOT_USERNAME:
            me = await client.get_me()
            BOT_USERNAME = me.username
            
        await message.reply_text(
            START_MESSAGE.format(user_name=user.first_name or "User"),
            reply_markup=build_start_keyboard(BOT_USERNAME),
            disable_web_page_preview=True
        )
    except Exception as e:
        log.warning(f"Failed to respond to /start for {user.id}: {e}")


@app.on_callback_query(filters.regex(r"^status_check$"))
async def status_checker(client: Client, callback_query):
    await callback_query.answer(
        f"🚀 Bot Active | Users: {len(USER_DATABASE)} | Cleaner: {app._cleaner_task_started} | Processed Chats: {len(PROCESSED_CHATS)}",
        show_alert=True
    )


# ---------------- Auto-Approve New Join Requests (Instant) ---------------- #
@app.on_chat_join_request()
async def auto_approve(client: Client, req: ChatJoinRequest):
    user = req.from_user
    chat = req.chat

    if AUTO_APPROVE_CHAT_ID and chat.id != AUTO_APPROVE_CHAT_ID:
        log.debug(f"Ignoring join request from chat {chat.id} (not configured)")
        return

    log.info(f"➡️ Processing NEW join request: {user.first_name} ({user.id}) -> {chat.title}")

    success = await approve_request_with_dm(
        client,
        chat.id,
        user.id,
        user.first_name or "User",
        chat.title or "this chat"
    )
    
    if not success:
        log.warning(f"⚠️ Failed to approve {user.id} in {chat.id}")


# ---------------- Manual approve command (admins only) ---------------- #
@app.on_message(filters.command("approve") & filters.group)
async def manual_approve_handler(client: Client, message: Message):
    if not await is_admin_or_creator(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ Yeh command sirf admins ke liye hai.")
        return

    target_user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1 and message.command[1].lstrip("-").isdigit():
        target_user_id = int(message.command[1])
    else:
        await message.reply_text("❓ Reply karein user ko ya `/approve <user_id>` dein.")
        return

    try:
        approved_user = await client.get_users(target_user_id)
        success = await approve_request_with_dm(
            client,
            message.chat.id,
            target_user_id,
            approved_user.first_name or "User",
            message.chat.title
        )
        
        if success:
            await message.reply_text(f"✅ {approved_user.first_name} ({approved_user.id}) approved!")
        else:
            await message.reply_text(f"⚠️ Could not approve {target_user_id}")

    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")


# ---------------- Clear Command (admins only) ---------------- #
@app.on_message(filters.command("clear") & filters.group)
async def clear_command_handler(client: Client, message: Message):
    """Admin command to manually clear all pending requests."""
    if not await is_admin_or_creator(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ Yeh command sirf admins ke liye hai.")
        return
    
    status_msg = await message.reply_text("🧹 Clearing all pending requests...")
    
    approved = await clear_pending_requests(client, message.chat.id, message.chat.title)
    
    await status_msg.edit_text(f"✅ Cleared {approved} pending requests!")


# ---------------- Broadcast (developer only) ---------------- #
@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client: Client, message: Message):
    if not DEVELOPER_ID:
        await message.reply_text("⚠️ Broadcasting is disabled (DEVELOPER_ID not configured).")
        return

    if message.from_user.id != DEVELOPER_ID:
        await message.reply_text("⛔ Yeh command sirf developer ke liye hai.")
        return

    if not message.reply_to_message:
        await message.reply_text("❓ Reply karein us message ko jise aap broadcast karna chahte hain.")
        return

    broadcast_message = message.reply_to_message
    total = len(USER_DATABASE)
    await message.reply_text(f"🚀 Broadcast starting — {total} users")

    sent = 0
    failed = 0
    for uid in list(USER_DATABASE):
        try:
            await broadcast_message.copy(uid)
            sent += 1
            await asyncio.sleep(0.05)
        except FloodWait as fw:
            log.warning(f"⏳ FloodWait during broadcast: sleeping {fw.value}s")
            await asyncio.sleep(fw.value)
            try:
                await broadcast_message.copy(uid)
                sent += 1
            except Exception:
                failed += 1
        except (UserIsBlocked, UserNotParticipant, PeerIdInvalid, RPCError):
            USER_DATABASE.discard(uid)
            failed += 1
        except Exception as e:
            log.error(f"Error broadcasting to {uid}: {e}")
            failed += 1

    await message.reply_text(f"✅ Broadcast complete. Sent: {sent}, Failed: {failed}")


# ---------------- Startup Function ---------------- #
async def start_bot_and_tasks():
    """Start bot and initialize all background tasks."""
    global BOT_USERNAME
    
    # 1. Start client
    await app.start()
    log.info("✅ Client connected to Telegram")
    
    # 2. Set bot username
    me = await app.get_me()
    BOT_USERNAME = me.username
    log.info(f"🤖 Bot Username: @{BOT_USERNAME}")

    # 3. Start background cleaner task
    if not app._cleaner_task_started:
        log.info("🚀 Starting background cleaner task...")
        asyncio.create_task(pending_requests_cleaner(app))
        app._cleaner_task_started = True
        log.info("✅ Background cleaner task started successfully")

    # 4. Optional: Clear pending requests from configured channel on startup
    if AUTO_APPROVE_CHAT_ID:
        log.info(f"🧹 Performing initial cleanup for configured chat: {AUTO_APPROVE_CHAT_ID}")
        await clear_pending_requests(app, AUTO_APPROVE_CHAT_ID)

    # Keep bot running
    await app.idle()


# ---------------- Run FastAPI ---------------- #
def run_fastapi():
    """FastAPI health check server in separate thread."""
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info")


# ---------------- Main Entry Point ---------------- #
if __name__ == "__main__":
    log.info("🚀 Starting Ultra Auto-Approver Bot")

    # Start health check server in background
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Run Pyrogram bot
    try:
        asyncio.run(start_bot_and_tasks())
    except KeyboardInterrupt:
        log.info("⌛ Shutting down (KeyboardInterrupt)")
    except Exception as e:
        log.error(f"🔥 Fatal error: {e}")
