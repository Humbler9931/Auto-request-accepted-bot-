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
    # Channel ID to auto-approve (if provided). Use env var name CHANNEL_ID for compatibility.
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

# ---------------- FastAPI Health-check ---------------- #
web_app = FastAPI()


@web_app.get("/")
def home():
    return {
        "status": "✅ Bot is running",
        "auto_approve_chat_id": AUTO_APPROVE_CHAT_ID or "ALL",
        "users_tracked": len(USER_DATABASE)
    }


# ---------------- Helper Functions ---------------- #
async def is_admin_or_creator(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        # member.status can be 'creator', 'administrator', 'member', 'restricted', etc.
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.debug(f"Could not check admin status for {user_id} in {chat_id}: {e}")
        return False


def build_start_keyboard(bot_username: Optional[str]) -> InlineKeyboardMarkup:
    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else "https://t.me/your_bot_here?startgroup=true"
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
    # Prefer the configured mandatory channel link
    channel_btn = InlineKeyboardButton("📣 Main Channel", url=CHANNEL_LINK)

    add_group_link = f"https://t.me/{bot_username}?startgroup=true" if bot_username else f"https://t.me/your_bot_here?startgroup=true"

    return InlineKeyboardMarkup([
        [channel_btn, InlineKeyboardButton("➕ Bot Ko Group Mein Jorein", url=add_group_link)],
        [InlineKeyboardButton("📚 Rules", url=RULES_LINK), InlineKeyboardButton("🛠️ Support", url=SUPPORT_LINK)]
    ])


# ---------------- Handlers ---------------- #
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
        f"🚀 Bot Active | Total Users Tracked: {len(USER_DATABASE)}",
        show_alert=True
    )


# ---------------- Auto-approve join requests ---------------- #
@app.on_chat_join_request()
async def auto_approve(client: Client, req: ChatJoinRequest):
    user = req.from_user
    chat = req.chat

    # If AUTO_APPROVE_CHAT_ID is set, only process that chat
    if AUTO_APPROVE_CHAT_ID and chat.id != AUTO_APPROVE_CHAT_ID:
        log.debug(f"Ignoring join request from chat {chat.id} because AUTO_APPROVE_CHAT_ID is set.")
        return

    log.info(f"➡️ Processing join request: user={user.id} ({user.first_name}) chat={chat.id} ({chat.title})")
    request_key = (chat.id, user.id)
    PENDING_REQUESTS[request_key] = time.time()

    # Add to in-memory DB
    USER_DATABASE.add(user.id)

    # Approve request
    try:
        await client.approve_chat_join_request(chat.id, user.id)
        PENDING_REQUESTS.pop(request_key, None)
        log.info(f"✅ Approved join request: {user.id} -> {chat.title} ({chat.id})")
    except RPCError as e:
        log.error(f"❌ RPCError while approving {user.id} for chat {chat.id}: {e}")
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
    except FloodWait as fw:
        log.warning(f"⏳ FloodWait when sending PM to {user.id}: sleeping {fw.value}s")
        await asyncio.sleep(fw.value)
        try:
            await client.send_message(
                user.id,
                WELCOME_TEXT.format(user_name=user.first_name or "Friend", chat_title=chat.title or "this chat", mandatory_channel=MANDATORY_CHANNEL),
                reply_markup=get_welcome_keyboard(chat, BOT_USERNAME)
            )
            log.info(f"✉️ Sent welcome PM to {user.id} after waiting")
        except Exception as e2:
            log.warning(f"⚠️ Retry failed sending PM to {user.id}: {e2}")
    except (PeerIdInvalid, UserIsBlocked, UserNotParticipant):
        # Can't PM user (blocked or hasn't started bot)
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
    # Admin check
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
