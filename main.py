import os
import logging
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton

# Logging setup (Debug messages dekhne ke liye)
logging.basicConfig(level=logging.INFO)

# --- Environment Variables Load Karna ---
# .env file se variables load karte hain
load_dotenv()

# Variables ko Environment se fetch karte hain
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN") 
CHANNEL_ID = os.getenv("CHANNEL_ID") 
MANDATORY_CHANNEL = os.getenv("MANDATORY_CHANNEL", "@narzoxbot") # Default value agar set na ho

# Mandatory channel ka link buttons ke liye
CHANNEL_LINK = f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"

# Target filter set karna. Agar CHANNEL_ID set nahi hai, toh bot sabhi chats ke liye kaam karega.
# ID ko int mein convert karna zaroori hai.
TARGET_FILTER = filters.chat(int(CHANNEL_ID)) if CHANNEL_ID and CHANNEL_ID.isdigit() else None

# --- Advanced Welcome Message aur Buttons ---

WELCOME_TEXT = (
    "⚜️ **🔥 N A R Z O X C O M M U N I T Y 🔥** ⚜️\n\n"
    "🎉 **Badhai ho!** Aapka **Join Request** safaltapoorvak **Accept** kar liya gaya hai. "
    "Aap ab hamari **Advanced Community** ke sadasya hain.\n\n"
    "🔔 **Zaroori Nirdesh:**\n"
    "Community ke saare **Premium Features** aur **Tools** ko istemal karne ke liye, "
    "kripya niche diye gaye **Narzox Bot** ko **Start** karein aur hamare channel ko **Join** karein."
)

WELCOME_KEYBOARD = InlineKeyboardMarkup([
    [
        # Narzox Bot ko start karne ka button
        InlineKeyboardButton("🤖 Narzox Bot Ko Start Karein", url=f"https://t.me/narzoxbot?start=welcome"),
    ],
    [
        # Mandatory Channel join karne ka button
        InlineKeyboardButton("📣 Bot Channel Join Karein", url=CHANNEL_LINK),
        # Rules ya Support group ka button
        InlineKeyboardButton("🤝 Support / Rules", url=CHANNEL_LINK), 
    ]
])

# --- Initialize Client ---
try:
    bot = Client(
        "auto_approver_session", # Session name
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )
except Exception as e:
    logging.error(f"Initialization Error: Ensure API_ID, API_HASH, and BOT_TOKEN are correctly set in .env. {e}")
    exit(1)


@bot.on_chat_join_request(TARGET_FILTER)
async def handle_join_request(client: Client, update: ChatJoinRequest):
    """
    Channel join request ko handle karta hai.
    """
    user = update.from_user
    chat = update.chat
    
    logging.info(f"New join request from {user.id} for chat {chat.title}")
    
    # 1. Request ko Approve karna
    try:
        await client.approve_chat_join_request(
            chat_id=chat.id, 
            user_id=user.id
        )
        logging.info(f"✅ Approved: {user.first_name}")

    except Exception as e:
        logging.error(f"❌ Failed to approve request for {user.id}. Bot is likely NOT ADMIN or missing 'Add Members' permission. Error: {e}")
        return # Agar approve nahi hua, to aage mat badho
        

    # 2. Channel mein Stylish Welcome Message Bhejna
    # Yeh message channel mein dikhega.
    try:
        await client.send_message(
            chat_id=chat.id, 
            text=WELCOME_TEXT,
            reply_markup=WELCOME_KEYBOARD,
            parse_mode="markdown"
        )
        logging.info(f"✉️ Stylish welcome message sent to channel.")

    except Exception as e:
        logging.error(f"⚠️ Could not send channel welcome message. Bot may be missing 'Post Messages' permission. Error: {e}")
        
    
    # 3. User ko Personal Message (PM) Bhejna (Optional, Jaise aapne pucha tha)
    try:
        pm_message = f"Hey **{user.first_name}**, aapki request **{chat.title}** channel ke liye **Accept** ho gayi hai! 🥳"
        
        # User ko stylish message bhejne ki koshish (Agar user ne bot ko start kiya hai)
        await client.send_message(
            chat_id=user.id, 
            text=pm_message,
            parse_mode="markdown"
        )
        logging.info(f"✉️ PM sent to user: {user.first_name}")

    except Exception as e:
        # Agar user ne bot start nahi kiya hai, to yeh message fail hoga, aur yeh theek hai.
        logging.warning(f"⚠️ Could not send PM to {user.first_name}. User has not started the bot or privacy is strict.")
        

# --- Start Bot ---
if __name__ == "__main__":
    print("🚀 Bot starting... (Check logs for details)")
    bot.run()
