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
    # Note: Accessing app._cleaner_task_started here is safe because it's set before the main app.run() loop starts its worker threads.
    return {
        "status": "✅ Bot is Running (via FastAPI)",
        "auto_approve_chat_id": AUTO_APPROVE_CHAT_ID or "ALL (Using Safe Dialog Check)",
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
    channel_btn = InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK)
    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else f"https://t.me/your_bot_here?startgroup=true"

    return InlineKeyboardMarkup([
        [channel_btn, InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=add_group_link)],
        [InlineKeyboardButton("📚 Rules", url=RULES_LINK), InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)]
    ])


# ---------------- Scheduled Background Cleaner Task ---------------- #
async def pending_requests_cleaner(client: Client):
    """
    Background task to check and clear already pending requests periodically (every 5 mins).
    This handles requests that arrived while the bot was offline or asleep (due to Render).
    """
    global BOT_USERNAME
    
    while True:
        # Wait 5 minutes. This delay is non-blocking.
        await asyncio.sleep(300)
        log.info("🧹 Starting scheduled check for already pending requests...")
        
        chats_to_check: Set[int] = set()
        
        if AUTO_APPROVE_CHAT_ID:
            # Check only the configured chat
            chats_to_check.add(AUTO_APPROVE_CHAT_ID)
            log.debug(f"Checking only the configured chat: {AUTO_APPROVE_CHAT_ID}")
        else:
            # Check a limited number of recent chats (safer for high chat count)
            try:
                # Fetch recent 500 dialogs to find potential target chats
                async for dialog in client.get_dialogs(limit=500):
                    if dialog.chat.type in ["channel", "supergroup"]:
                        chats_to_check.add(dialog.chat.id)
                log.info(f"Found {len(chats_to_check)} active chats/channels to check.")
            except Exception as e:
                log.error(f"Error getting dialogs for cleaner: {e}")

        # Process pending requests for each identified chat
        total_approved = 0
        for chat_id in chats_to_check:
            approved_count = 0
            
            try:
                # Get and approve up to 50 pending requests per cycle per chat
                async for req in client.get_chat_join_requests(chat_id, limit=50):
                    await client.approve_chat_join_request(chat_id, req.user.id)
                    USER_DATABASE.add(req.user.id)
                    approved_count += 1
                
                if approved_count > 0:
                    log.info(f"✅ Auto-cleaned {approved_count} pending requests in chat {chat_id}")
                    total_approved += approved_count
                    
            except FloodWait as fw:
                log.warning(f"⏳ FloodWait during cleaner for {chat_id}: sleeping {fw.value}s")
                await asyncio.sleep(fw.value)
            except RPCError as e:
                # Bot might have lost 'Manage Invite Links' permission in this chat
                log.debug(f"⚠️ RPCError while auto-cleaning {chat_id}: {e}")
            except Exception as e:
                log.error(f"❌ Unexpected error in auto-cleaner for {chat_id}: {e}")

        if total_approved > 0:
            log.info(f"🎉 Scheduled check finished. Total approved: {total_approved}")
        else:
            log.info("🧹 Scheduled check finished. No pending requests found.")

# ---------------- Startup Hook ---------------- #
@app.on_message(filters.me)
async def startup_cleaner_scheduler(client: Client, message: Message):
    """
    Ensures global BOT_USERNAME is set and the background cleaner task starts only once.
    This fires when the bot receives its own messages (filters.me) or sends its first message.
    """
    global BOT_USERNAME
    
    # 1. Set BOT_USERNAME once
    if not BOT_USERNAME:
        try:
            me = await client.get_me()
            BOT_USERNAME = me.username
            log.info(f"Bot Username set to: @{BOT_USERNAME}")
        except Exception:
             pass 

    # 2. Start the background task only once
    if not client._cleaner_task_started:
        log.info("Starting initial checks and background pending requests cleaner task...")
        # Start the task but don't wait for it
        asyncio.create_task(pending_requests_cleaner(client))
        client._cleaner_task_started = True
        log.info("Background pending cleaner task started successfully.")
    
    # Optional: Prevent filters.me from spamming the console for status checks
    if message.command and message.command[0] in ["start", "status"]:
        return


# ---------------- Handlers (Unchanged for Functionality) ---------------- #
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    # add to DB
    USER_DATABASE.add(user.id)
    log.info(f"🆕 /start from {user.id} — added to USER_DATABASE (count={len(USER_DATABASE)})")

    try:
        me = await client.get_me()
        bot_username_local = me.username or None
        await message.reply_text(
            START_MESSAGE.format(user_name=user.first_name or "User"),
            reply_markup=build_start_keyboard(bot_username_local),
            disable_web_page_preview=True
        )
    except Exception as e:
        log.warning(f"Failed to respond to /start for {user.id}: {e}")


@app.on_callback_query(filters.regex(r"^status_check$"))
async def status_checker(client: Client, callback_query):
    await callback_query.answer(
        f"🚀 Bot Active | Total Users Tracked: {len(USER_DATABASE)} | Cleaner Active: {app._cleaner_task_started}",
        show_alert=True
    )

# ----------------------------------------------
## Auto-Approve Join Requests (Universal & Instant)
# ----------------------------------------------
@app.on_chat_join_request()
async def auto_approve(client: Client, req: ChatJoinRequest):
    # Logic remains robust for instant approval of *new* requests.
    user = req.from_user
    chat = req.chat

    if AUTO_APPROVE_CHAT_ID and chat.id != AUTO_APPROVE_CHAT_ID:
        log.debug(f"Ignoring join request from chat {chat.id} because AUTO_APPROVE_CHAT_ID is set and doesn't match.")
        return

    log.info(f"➡️ Processing INSTANT join request: user={user.id} chat={chat.id}")
    request_key = (chat.id, user.id)
    PENDING_REQUESTS[request_key] = time.time()

    USER_DATABASE.add(user.id)

    try:
        await req.approve()
        PENDING_REQUESTS.pop(request_key, None)
        log.info(f"✅ Approved INSTANT join request: {user.id} -> {chat.title}")
    except RPCError as e:
        log.error(f"❌ RPCError while approving {user.id} for chat {chat.id}: {e} (Check 'Manage Invite Links' permission)")
        return
    except Exception as e:
        log.error(f"❌ Unexpected error while approving join request: {e}")
        return

    # Try sending private welcome message
    try:
        me = await client.get_me()
        bot_username_local = me.username or BOT_USERNAME or None
        await client.send_message(
            user.id,
            WELCOME_TEXT.format(user_name=user.first_name or "Friend", chat_title=chat.title or "this chat", mandatory_channel=MANDATORY_CHANNEL),
            reply_markup=get_welcome_keyboard(chat, bot_username_local)
        )
        log.info(f"✉️ Sent welcome PM to {user.id}")
    except (PeerIdInvalid, UserIsBlocked, UserNotParticipant):
        log.info(f"⚠️ Could not PM user {user.id} — sending a fallback message to the chat.")
        try:
            mention = user.mention if hasattr(user, "mention") else f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
            await client.send_message(chat.id, f"Welcome {mention}! ✅", parse_mode="html")
        except Exception as e:
            log.debug(f"Failed to send fallback chat message in {chat.id}: {e}")
    except Exception as e:
        log.warning(f"⚠️ Failed to send PM to {user.id}: {e}")


# ---------------- Manual approve command (admins only) ---------------- #
@app.on_message(filters.command("approve") & filters.group)
async def manual_approve_handler(client: Client, message: Message):
    # This command remains useful for manual quick fixes.
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
        await client.approve_chat_join_request(message.chat.id, target_user_id)
        PENDING_REQUESTS.pop((message.chat.id, target_user_id), None)
        approved_user = await client.get_users(target_user_id)
        await message.reply_text(f"✅ {approved_user.first_name} ({approved_user.id}) ko approve kar diya gaya.")
        USER_DATABASE.add(target_user_id)

        # Try to PM
        try:
            me = await client.get_me()
            bot_username_local = me.username or BOT_USERNAME or None
            await client.send_message(
                target_user_id,
                WELCOME_TEXT.format(user_name=approved_user.first_name or "Friend", chat_title=message.chat.title, mandatory_channel=MANDATORY_CHANNEL),
                reply_markup=get_welcome_keyboard(message.chat, bot_username_local)
            )
        except Exception as e:
            log.debug(f"Could not send PM after manual approval to {target_user_id}: {e}")

    except RPCError as e:
        await message.reply_text(f"❌ Approval failed (RPCError): {e}")
    except Exception as e:
        await message.reply_text(f"❌ Approval failed: {e}")


# ---------------- Broadcast (developer only) ---------------- #
@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client: Client, message: Message):
    # This remains unchanged.
    if not DEVELOPER_ID:
        await message.reply_text("⚠️ Broadcasting is disabled on this bot (DEVELOPER_ID not configured).")
        return

    if message.from_user.id != DEVELOPER_ID:
        await message.reply_text("⛔ Yeh command sirf developer ke liye hai.")
        return

    if not message.reply_to_message:
        await message.reply_text("❓ Reply karein us message ko jise aap broadcast karna chahte hain.")
        return

    broadcast_message = message.reply_to_message
    total = len(USER_DATABASE)
    await message.reply_text(f"🚀 Broadcast shuru ho raha hai — {total} users ko bheja jayega.")

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
        except (UserIsBlocked, UserNotParticipant, PeerIdInvalid, RPCError) as e:
            USER_DATABASE.discard(uid)
            failed += 1
        except Exception as e:
            log.error(f"Error broadcasting to {uid}: {e}")
            failed += 1

    await message.reply_text(f"✅ Broadcast complete. Sent: {sent}, Failed/Removed: {failed}, Current tracked: {len(USER_DATABASE)}")


# ---------------- Run ---------------- #
def run_fastapi():
    """FastAPI health check server ko separate thread mein chalaata hai."""
    uvicorn.run(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info")


if __name__ == "__main__":
    log.info("🚀 Starting Bot — FastAPI healthcheck + Pyrogram bot")

    # Start health check server in background thread
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Run pyrogram (blocks) - FIX: app.idle() ki jagah blocking app.run() ka istemal kiya gaya hai.
    try:
        log.info("Client is starting now...")
        # app.run() client ko start karta hai aur use tab tak chalu rakhta hai (app.idle() ka kaam karta hai)
        app.run()
    except KeyboardInterrupt:
        log.info("⌛ Shutting down (KeyboardInterrupt)")
    except Exception as e:
        log.error(f"🔥 Fatal error running pyrogram client: {e}")
