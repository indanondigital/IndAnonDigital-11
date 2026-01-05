import os
import asyncio
import random
import uvicorn
import hmac
import hashlib
import json
import datetime
import re
import string
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Telegram Imports
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationBuilder
from telegram.helpers import escape_markdown

# Local DB Import
from db import db

# FastAPI Imports
from fastapi import FastAPI, Request, HTTPException

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()

# Load Secrets
BOT_TOKEN = os.getenv("BOT_TOKEN")
# ✅ Correct
ADMIN_ID = int(os.getenv("ADMIN_ID"))


# --- LOGGING CONFIGURATION ---
try:
    # 1. REPORT CHANNEL (Bans, User Reports, Appeals)
    LOG_REPORTS = int(os.getenv("LOG_CHANNEL_REPORTS")) 
    
    # 2. MEDIA CHANNEL (Photos, Videos, Evidence)
    LOG_MEDIA = int(os.getenv("LOG_CHANNEL_MEDIA"))
    
    # 3. PAYMENT CHANNEL (Money, VIP Subscriptions)
    LOG_PAYMENTS = int(os.getenv("LOG_CHANNEL_PAYMENTS"))
except (TypeError, ValueError):
    print("⚠️ [LOG] WARNING: One or more Log Channel IDs are missing/invalid.")
    LOG_REPORTS = LOG_MEDIA = LOG_PAYMENTS = 0

# --- LOGGING HELPER FUNCTIONS ---

async def send_report_log(context, message):
    """Sends logs to the REPORTS channel."""
    if LOG_REPORTS:
        try: await context.bot.send_message(chat_id=LOG_REPORTS, text=message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: print(f"❌ Report Log Error: {e}")

async def send_media_log(context, caption, photo=None, video=None, voice=None, audio=None, video_note=None, document=None):
    """Sends logs to the MEDIA channel."""
    if LOG_MEDIA:
        try:
            if photo:
                await context.bot.send_photo(chat_id=LOG_MEDIA, photo=photo, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            elif video:
                await context.bot.send_video(chat_id=LOG_MEDIA, video=video, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            elif voice:
                await context.bot.send_voice(chat_id=LOG_MEDIA, voice=voice, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            elif audio:
                await context.bot.send_audio(chat_id=LOG_MEDIA, audio=audio, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            elif video_note:
                # Video Notes (Round) cannot have captions. Send text first, then video.
                await context.bot.send_message(chat_id=LOG_MEDIA, text=caption, parse_mode=ParseMode.MARKDOWN_V2)
                await context.bot.send_video_note(chat_id=LOG_MEDIA, video_note=video_note)
            elif document:
                await context.bot.send_document(chat_id=LOG_MEDIA, document=document, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await context.bot.send_message(chat_id=LOG_MEDIA, text=caption, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: print(f"❌ Media Log Error: {e}")

async def send_payment_log(context, message):
    """Sends logs to the PAYMENTS channel."""
    if LOG_PAYMENTS:
        try: await context.bot.send_message(chat_id=LOG_PAYMENTS, text=message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: print(f"❌ Payment Log Error: {e}")

# --- 🔒 PAYMENT SHADOW LOG ---
async def log_payment_event(context, user_id, amount, status, payload):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = (
        f"🧾 **PAYMENT VERIFIED**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 **User:** `{user_id}`\n"
        f"💰 **Amount:** {amount}\n"
        f"📊 **Status:** {status}\n"
        f"🆔 **Ref:** `{payload}`\n"
        f"🕒 **Time:** `{timestamp}`\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    # Uses the new specific Payment Channel
    await send_payment_log(context, log_msg)


# --- GLOBAL STATE ---
user_states = {}
active_sessions = {}
reporting_cache = {}  # <--- NEW: Stores {reporter_id: bad_user_id}
last_partners = {}   # <--- NEW: Remembers the last person you chatted with
user_preferences = {} # <--- NEW: Stores user search preference (male/female/any)

# --- GLOBAL DATA ---
INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", 
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", 
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", 
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", 
    "Uttarakhand", "West Bengal", "Delhi", "Jammu & Kashmir"
]

COUNTRIES = ["India 🇮🇳", "USA 🇺🇸", "UK 🇬🇧", "Canada 🇨🇦", "Other 🌍"]

# --- KEYBOARD GENERATORS ---
def get_gender_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Male ♂️", callback_data="reg_gender_male"),
         InlineKeyboardButton("Female ♀️", callback_data="reg_gender_female")]
    ])

def get_country_kb():
    keyboard = []
    row = []
    for country in COUNTRIES:
        row.append(InlineKeyboardButton(country, callback_data=f"reg_country_{country.split()[0]}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    # Add Manual Entry Option
    keyboard.append([InlineKeyboardButton("✍️ Type Manually", callback_data="reg_manual_entry")])
    return InlineKeyboardMarkup(keyboard)

def get_indian_states_kb(page=0):
    states_per_page = 10
    start = page * states_per_page
    end = start + states_per_page
    current_states = INDIAN_STATES[start:end]
    
    keyboard = []
    row = []
    
    # 1. State Buttons
    for state in current_states:
        row.append(InlineKeyboardButton(state, callback_data=f"reg_state_{state}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)

    # 2. Navigation Buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"reg_page_{page-1}"))
    if end < len(INDIAN_STATES):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"reg_page_{page+1}"))
    if nav_row: keyboard.append(nav_row)

    # 3. Manual Entry Button
    keyboard.append([InlineKeyboardButton("✍️ Type Manually", callback_data="reg_manual_entry")])
    
    return InlineKeyboardMarkup(keyboard)

# Helper to send final Welcome/Status message
async def send_welcome(context, user_id):
    status = "Free Member"
    if await db.check_premium(user_id): 
        status = "🌟 VIP Member"
    
    if user_id == ADMIN_ID: 
        status = "👑 Superuser (Lifetime Free)"
        
    await context.bot.send_message(
        user_id, 
        f"✅ **Registration Complete!**\n\nYour Status: **{status}**\n\nUse /chat to start matching.", 
        reply_markup=main_menu,
        parse_mode=ParseMode.MARKDOWN
    )

# --- 2. GLOBAL STATE ---
user_states = {}
active_sessions = {} 

VIP_PLANS = {
    "pay_1m":  {"amt": 20000,  "days": 30,  "lbl": "1 Month"},
    "pay_3m":  {"amt": 50000,  "days": 90,  "lbl": "3 Months"},
    "pay_6m":  {"amt": 105000, "days": 180, "lbl": "6 Months"},
    "pay_12m": {"amt": 209900, "days": 365, "lbl": "12 Months"}
}
AMT_TO_DAYS = {plan['amt']: plan['days'] for plan in VIP_PLANS.values()}

# --- 3. KEYBOARDS ---

# --- MAIN MENU (Clean Version) ---
main_menu = ReplyKeyboardMarkup([
    [KeyboardButton("💬 Chat"), KeyboardButton("🔄 Re-Chat")],
    # "Profile" is gone. Merged into Settings below.
    [KeyboardButton("⚙️ Settings"), KeyboardButton("💎 Premium")], 
    [KeyboardButton("❓ Help"), KeyboardButton("ℹ️ About")]
], resize_keyboard=True)

stop_menu = ReplyKeyboardMarkup([
    [KeyboardButton("❌ Exit Chat"), KeyboardButton("🚨 Report Partner")]
], resize_keyboard=True)

search_menu = ReplyKeyboardMarkup([
    [KeyboardButton("🎲 Random"), KeyboardButton("👩 Girls (VIP)"), KeyboardButton("👨 Boys (VIP)")],
    [KeyboardButton("🔙 Back")]
], resize_keyboard=True)

# --- 4. HELPER FUNCTIONS ---

def log(user_id, action, **kwargs):
    """
    Logs actions in the specific Railway format.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_string = f"[LOG] {timestamp} | User: {user_id} | Action: {action}"
    for key, value in kwargs.items():
        log_string += f" | {key}: {value}"
    print(log_string, flush=True)

def generate_session_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_random_contact():
    first = random.choice(['6', '7', '8', '9'])
    rest = ''.join([str(random.randint(0, 9)) for _ in range(9)])
    return first + rest

# --- 5. UI GENERATORS (SAFE MODE) ---

# [PASTE THIS INTO main.py - REPLACING THE OLD send_match_messages]

async def send_match_messages(context, user1_id, user2_id):
    """Sends the formatted match message SECURELY and LOGS the match."""
    
    session_id = generate_session_id()
    start_time = datetime.datetime.now()
    active_sessions[user1_id] = {"start_time": start_time, "session_id": session_id}
    active_sessions[user2_id] = {"start_time": start_time, "session_id": session_id}

    # --- LOGGING THE MATCH ---
    log(user1_id, "MATCH_FOUND", with_user=user2_id, session=session_id)
    log(user2_id, "MATCH_FOUND", with_user=user1_id, session=session_id)
    # -------------------------

    u1_data = await db.get_user(user1_id)
    u2_data = await db.get_user(user2_id)
    
    # Check VIP Status
    u1_vip = await db.check_premium(user1_id) or user1_id == ADMIN_ID
    u2_vip = await db.check_premium(user2_id) or user2_id == ADMIN_ID

    def safe(text):
        return escape_markdown(str(text), version=2)

    # --- LOGIC FOR USER 1 (What User 1 sees about User 2) ---
    real_gender_2 = safe(u2_data['gender'].capitalize())
    
    # If User 1 is VIP -> Show Gender. 
    # If Free -> Show Blurred "👑 FOR Premium Users Only" text.
    if u1_vip:
        g2_display = real_gender_2
    else:
        # The '||' creates the Blur effect.
        # When clicked, it reveals "👑 FOR Premium Users Only"
        g2_display = "||👑 FOR Premium Users Only||"

    msg_to_u1 = (
        f"✅ *Partner Matched*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌍 *Country:* {safe(u2_data['country'])}\n"
        f"👥 *Gender:* {g2_display}\n" # This will be blurred for free users
        f"📣 *Age:* {safe(u2_data['age'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🚫 _Links are restricted_\n"
        f"⏱️ _Media sharing unlocked after 2 minutes_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/exit \- Leave the chat" 
    )

    # --- LOGIC FOR USER 2 (What User 2 sees about User 1) ---
    real_gender_1 = safe(u1_data['gender'].capitalize())
    
    # If User 2 is VIP -> Show Gender.
    if u2_vip:
        g1_display = real_gender_1
    else:
        g1_display = "|| 👑 FOR Premium Users Only ||"

    msg_to_u2 = (
        f"✅ *Partner Matched*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌍 *Country:* {safe(u1_data['country'])}\n"
        f"👥 *Gender:* {g1_display}\n" # This will be blurred for free users
        f"📣 *Age:* {safe(u1_data['age'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🚫 _Links are restricted_\n"
        f"⏱️ _Media sharing unlocked after 2 minutes_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/exit \- Leave the chat"
    )

    await context.bot.send_message(user1_id, msg_to_u1, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=stop_menu)
    await context.bot.send_message(user2_id, msg_to_u2, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=stop_menu)

# --- 🛡️ SECURITY & GROUP MODERATION SYSTEM ---

# A simple list of bad words (You can expand this)
# --- 🛡️ SECURITY & GROUP MODERATION SYSTEM ---

# Load bad words from .env file
# 1. Get the string from .env
bad_words_env = os.getenv("BAD_WORDS_LIST", "") 

# 2. Convert comma-separated string back into a Python List
if bad_words_env:
    BAD_WORDS = [word.strip() for word in bad_words_env.split(",")]
else:
    # Fallback backup list (just in case .env is empty)
    BAD_WORDS = ["scam", "fraud", "kill", "abuse"]

# In-Memory Warning Tracker: {user_id: warning_count}
group_warnings = {}

async def group_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Watches public group messages.
    - Warns users for bad words.
    - Bans them after 3 strikes.
    """
    message = update.message
    # Safety check: if message is None (e.g. edited post), skip
    if not message or not message.text:
        return

    user = update.effective_user
    chat = update.effective_chat
    
    # 1. Ignore Private Chats (Only watch Groups)
    if chat.type == "private":
        return 

    # 2. Ignore Admins (Don't ban the owner!)
    try:
        member = await chat.get_member(user.id)
        if member.status in ["administrator", "creator"]:
            return
    except:
        pass # If we can't check admin status, just continue

    text = message.text.lower()
    
    # 3. CHECK: Is there a bad word?
    found_bad_word = False
    for bad in BAD_WORDS:
        if bad in text:
            found_bad_word = True
            break
            
    # 4. PUNISHMENT LOGIC
    if found_bad_word:
        # Delete the bad message
        try: await message.delete()
        except: pass 
        
        # Increase Warning Count
        current_warns = group_warnings.get(user.id, 0) + 1
        group_warnings[user.id] = current_warns
        
        # STRIKE 1 or 2
        if current_warns < 3:
            remaining = 3 - current_warns
            msg = (
                f"⚠️ **WARNING {current_warns}/3**\n"
                f"👤 {user.mention_html()}\n\n"
                f"🚫 **Do not use bad language or harass others.**\n"
                f"You will be banned after {remaining} more warnings."
            )
            await context.bot.send_message(chat.id, msg, parse_mode=ParseMode.HTML)
            
        # STRIKE 3: BAN HAMMER 🔨
        else:
            try:
                # Ban user from the group
                await chat.ban_member(user.id)
                
                # Announce the ban
                msg = (
                    f"⛔ **BANNED**\n"
                    f"👤 {user.mention_html()} has been removed.\n"
                    f"Reason: Repeated violation of community rules."
                )
                await context.bot.send_message(chat.id, msg, parse_mode=ParseMode.HTML)
                
                # Reset warnings
                group_warnings.pop(user.id, None)
                
            except Exception as e:
                await context.bot.send_message(chat.id, f"⚠️ I tried to ban {user.name} but failed. (Am I admin?)")

# --- 6. CORE HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    log(user_id, "START_BOT")
    
    await db.add_user(user_id)
    
    # Force Registration Check
    is_complete = await check_registration(update, context, user_id)
    
    if is_complete:
        await send_welcome(context, user_id)

async def handle_registration_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    # --- 1. GENDER (Button Click) ---
    if data.startswith("reg_gender_"):
        gender = data.split("_")[-1]
        await db.set_gender(user_id, gender)
        log(user_id, "SET_GENDER", gender=gender)
        
        # ✅ CONFIRMATION MESSAGE
        await context.bot.send_message(user_id, f"✅ Gender Updated to **{gender.capitalize()}**!", parse_mode=ParseMode.MARKDOWN)
        
        await query.answer("Gender Saved")
        await query.message.delete()
        await check_registration(update, context, user_id)

    # --- 2. COUNTRY (Button Click) ---
    elif data.startswith("reg_country_"):
        country = data.split("_")[-1]
        if country == "India":
            # If India -> Show States
            await query.edit_message_text(
                "🇮🇳 **Select your State:**", 
                reply_markup=get_indian_states_kb(0),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            # Other Country -> Finish
            await db.set_country(user_id, country)
            log(user_id, "SET_LOCATION", location=country)
            
            # ✅ CONFIRMATION MESSAGE
            await context.bot.send_message(user_id, f"✅ Location Updated to **{country}**!", parse_mode=ParseMode.MARKDOWN)
            
            await query.answer("Location Saved")
            await query.message.delete()
            await send_welcome(context, user_id)

    # --- 3. STATE SELECTION (India) ---
    elif data.startswith("reg_state_"):
        state = data.replace("reg_state_", "")
        full_loc = f"India, {state}"
        await db.set_country(user_id, full_loc)
        log(user_id, "SET_LOCATION", location=full_loc)
        
        # ✅ CONFIRMATION MESSAGE
        await context.bot.send_message(user_id, f"✅ Location Updated to **{full_loc}**!", parse_mode=ParseMode.MARKDOWN)
        
        await query.answer("State Saved")
        await query.message.delete()
        await send_welcome(context, user_id)

    # --- 4. MANUAL ENTRY TRIGGER ---
    elif data == "reg_manual_entry":
        user_states[user_id] = "WAITING_MANUAL_LOC"
        await query.message.edit_text(
            "✍️ **Manual Entry**\n\nPlease type your City or Country name below:",
            parse_mode=ParseMode.MARKDOWN
        )
    
    # --- 5. PAGINATION ---
    elif data.startswith("reg_page_"):
        page = int(data.split("_")[-1])
        await query.edit_message_reply_markup(reply_markup=get_indian_states_kb(page))

    # --- 6. SETTINGS: RESET ACTIONS ---
    
    # User clicked "Update Gender"
    elif data == "reset_gender":
        await db.set_gender(user_id, None) 
        log(user_id, "RESET_PROFILE", field="gender")
        await query.message.delete()
        await check_registration(update, context, user_id) 

    # User clicked "Update Age"
    elif data == "reset_age":
        await db.set_age(user_id, None)
        log(user_id, "RESET_PROFILE", field="age")
        await query.message.delete()
        await check_registration(update, context, user_id)

    # User clicked "Update Location"
    elif data == "reset_loc":
        await db.set_country(user_id, None)
        log(user_id, "RESET_PROFILE", field="location")
        await query.message.delete()
        await check_registration(update, context, user_id)

    # Close Button
    elif data == "close_settings":
        await query.message.delete()

async def check_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    """
    Checks if user profile is complete. If not, prompts for missing data.
    """
    user_data = await db.get_user(user_id)
    
    # STEP 1: GENDER
    if not user_data.get('gender'):
        log(user_id, "PROMPT_REGISTRATION", step="gender")
        await context.bot.send_message(
            user_id, 
            "👋 **Welcome! Step 1/3**\n\nPlease select your Gender:", 
            reply_markup=get_gender_kb(),
            parse_mode=ParseMode.MARKDOWN
        )
        return False

    # STEP 2: AGE
    if not user_data.get('age'):
        log(user_id, "PROMPT_REGISTRATION", step="age")
        user_states[user_id] = "WAITING_AGE" # Enable text listener
        await context.bot.send_message(
            user_id, 
            "🎂 **Step 2/3: Age**\n\nPlease type your age (e.g., 24):",
            parse_mode=ParseMode.MARKDOWN
        )
        return False

    # STEP 3: LOCATION
    if not user_data.get('country'):
        log(user_id, "PROMPT_REGISTRATION", step="location")
        await context.bot.send_message(
            user_id, 
            "🌍 **Step 3/3: Location**\n\nWhere are you from?", 
            reply_markup=get_country_kb(),
            parse_mode=ParseMode.MARKDOWN
        )
        return False

    # ALL DONE
    user_states.pop(user_id, None) 
    return True

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 Bot Help\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        "💬 Chat - Start matching\n"
        "❌ Exit - End chat\n"
        "💎 Premium - See gender & unlimited chats\n"
        "💡 Rules: No links/media for first 2 mins."
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Support", url="https://t.me/YourSupportUser"), 
         InlineKeyboardButton("📢 Channel", url="https://t.me/Ind_AnonChatbotUpdates")]
    ])
    await update.message.reply_text(help_text, reply_markup=kb)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ About\n\nEnjoy free, anonymous one-to-one chats with real people. \nUpgrade anytime to unlock smart filters and preference-based matching. \n\n Made in India")

# --- BAN APPEAL HANDLER ---
async def handle_ban_appeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if query.data == "ban_appeal":
        # Log to REPORT CHANNEL
        await send_report_log(context, f"🆘 **BAN APPEAL**\n👤 User: `{user_id}`\n📝 Status: Requesting unban.")
        
        await query.answer("Appeal sent to Admin.", show_alert=True)
        await query.edit_message_text("✅ **Appeal Sent.**\nThe admin has been notified.")

# --- SUPPORT & CHANNEL SYSTEM ---

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User sends a message to Admin."""
    user_id = update.effective_user.id
    args = context.args # The text after /support
    
    if not args:
        await update.message.reply_text("⚠️ **Usage:** `/support Your Message Here`\nExample: `/support I found a bug!`", parse_mode=ParseMode.MARKDOWN)
        return

    message = " ".join(args)
    
    # 1. Notify Admin
    admin_text = (
        f"🆘 **SUPPORT REQUEST**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 From: `{user_id}`\n"
        f"📝 Message: {message}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👇 **To Reply:**\n"
        f"`/reply {user_id} YourResponse`"
    )
    
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ **Support Request Sent!**\nThe admin will reply shortly.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error sending to admin: {e}")

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin replies to a user."""
    # Security: Only Admin can use this
    if update.effective_user.id != ADMIN_ID: return

    try:
        # Args: /reply <user_id> <message>
        if len(context.args) < 2:
            await update.message.reply_text("⚠️ Usage: `/reply <user_id> <message>`", parse_mode=ParseMode.MARKDOWN)
            return

        target_id = int(context.args[0])
        reply_text = " ".join(context.args[1:])
        
        # Send to User
        await context.bot.send_message(
            chat_id=target_id,
            text=f"👨‍💻 **Admin Reply:**\n\n{reply_text}"
        )
        
        await update.message.reply_text(f"✅ Sent to {target_id}.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin posts to the Public Channel."""
    # Security: Only Admin can use this
    if update.effective_user.id != ADMIN_ID: return
    
    # Get Channel ID from env (Make sure you added it!)
    channel_id = os.getenv("CHANNEL_ID") 
    
    if not channel_id:
        await update.message.reply_text("⚠️ Error: `CHANNEL_ID` not set in .env")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/broadcast Your Message`")
        return

    message = " ".join(context.args)
    
    try:
        await context.bot.send_message(chat_id=int(channel_id), text=message)
        await update.message.reply_text("✅ **Posted to Channel!**")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 🛑 IGNORE GROUPS
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    # ... (Rest of code) ...

async def clear_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes the stuck menu buttons from the group."""
    msg = await update.message.reply_text(
        "🧹 Cleaning up buttons...", 
        reply_markup=ReplyKeyboardRemove()
    )
    # Delete the cleanup message after 2 seconds so chat stays clean
    await asyncio.sleep(2)
    await msg.delete()
    await update.message.delete()

# --- MASTER TEXT HANDLER ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 🛑 1. SECURITY: IGNORE GROUPS
    # This prevents the bot from sending menus or dating commands in public groups
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    text = update.message.text
    
    # ... (The rest of your existing code follows below) ...
    
    # 1. STRICT BAN CHECK
    if await db.is_banned(user_id):
        kb = [[InlineKeyboardButton("🆘 Contact Admin / Appeal", callback_data="ban_appeal")]]
        await update.message.reply_text("🚫 **YOU ARE BANNED**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    state = user_states.get(user_id)

    # ---------------------------------------------------------
    # 🚨 PRIORITY 1: EXIT CHAT
    # ---------------------------------------------------------
    if text in ["❌ Exit Chat", "/exit"]:
        if state:
            user_states.pop(user_id, None)
            reporting_cache.pop(user_id, None)

        if not await db.is_searching(user_id) and not await db.get_partner(user_id):
            await update.message.reply_text("⚠️ You are not in a chat.", reply_markup=main_menu)
            return

        partner_id = await db.disconnect(user_id)
        await db.remove_from_queue(user_id)
        
        active_sessions.pop(user_id, None)
        active_sessions.pop(partner_id, None)
        
        report_tag = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        last_partners[user_id] = {'id': partner_id, 'tag': report_tag}
        if partner_id:
            last_partners[partner_id] = {'id': user_id, 'tag': report_tag}

        kb_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Report User", callback_data=f"report_{report_tag}")],
            [InlineKeyboardButton("🗣 Find new partner", callback_data="find_new_partner")]
        ])

        msg_text = (
            "🚫 **You left the chat**\n"
            "____________________\n\n"
            f"⚠️ Report TAG: `{report_tag}`\n"
            "To report this user:\n"
            f"/report {report_tag}"
        )
        await update.message.reply_text(msg_text, reply_markup=kb_markup, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("👇 **Main Menu**", reply_markup=main_menu, parse_mode=ParseMode.MARKDOWN)

        if partner_id:
            partner_msg = (
                "🚫 **Partner left the chat**\n"
                "____________________\n\n"
                f"⚠️ Report TAG: `{report_tag}`\n"
                "To report this chat:\n"
                f"/report {report_tag}"
            )
            try: 
                await context.bot.send_message(partner_id, partner_msg, reply_markup=kb_markup, parse_mode=ParseMode.MARKDOWN)
                await context.bot.send_message(partner_id, "👇 **Main Menu**", reply_markup=main_menu, parse_mode=ParseMode.MARKDOWN)
            except: pass
            
        log(user_id, "EXIT_CHAT", tag=report_tag)
        return

    # ---------------------------------------------------------
    # 🚨 PRIORITY 2: CANCEL COMMAND
    # ---------------------------------------------------------
    if text in ["/cancel", "🔙 Cancel"]:
        if state == "WAITING_REPORT_REASON":
            user_states.pop(user_id, None)
            reporting_cache.pop(user_id, None)
            
            # 🛑 FIX APPLIED HERE: Check if in chat
            if await db.get_partner(user_id):
                await update.message.reply_text("🚫 Report cancelled. Continuing chat...", reply_markup=stop_menu)
            else:
                await update.message.reply_text("🚫 Report cancelled.", reply_markup=main_menu)
            return

        elif state in ["WAITING_AGE", "WAITING_MANUAL_LOC"]:
             user_states.pop(user_id, None)
             await update.message.reply_text("🚫 Action cancelled.", reply_markup=main_menu)
             return

    # ---------------------------------------------------------
    # 🚨 PRIORITY 3: CAPTURE REPORT REASON
    # ---------------------------------------------------------
    if state == "WAITING_REPORT_REASON":
        if text in ["💬 Chat", "⚙️ Settings", "💎 Premium", "❤️ Preferences"]:
            user_states.pop(user_id, None)
        else:
            report_data = reporting_cache.get(user_id)
            if not report_data:
                user_states.pop(user_id, None)
                await update.message.reply_text("❌ Error: Report session expired.", reply_markup=main_menu)
                return

            target_id = report_data['id']
            report_tag = report_data['tag']
            reason = text

            log_msg = (
                f"🚨 **USER REPORT**\n"
                f"━━━━━━━━━━\n"
                f"👮 **Reporter:** `{user_id}`\n"
                f"💀 **Accused:** `{target_id}`\n"
                f"🏷 **Tag:** `{report_tag}`\n"
                f"📝 **Reason:** `{escape_markdown(reason, version=2)}`\n"
                f"━━━━━━━━━━\n"
            )
            await send_report_log(context, log_msg)
            
            reporting_cache.pop(user_id, None)
            user_states.pop(user_id, None)
            
            # 🛑 FIX APPLIED HERE: Check if in chat
            if await db.get_partner(user_id):
                await update.message.reply_text("✅ **Report Submitted.**", reply_markup=stop_menu)
            else:
                await update.message.reply_text("✅ **Report Submitted.**", reply_markup=main_menu)
            return

    # ---------------------------------------------------------
    # 🚨 PRIORITY 4: MANUAL REPORT COMMAND
    # ---------------------------------------------------------
    if text == "🚨 Report Partner" or text.startswith("/report"):
        target_id = None
        target_tag = None
        
        current_partner = await db.get_partner(user_id)
        if current_partner:
            last = last_partners.get(user_id)
            if last and last['id'] == current_partner:
                target_id = last['id']
                target_tag = last['tag']
            else:
                 target_id = current_partner
                 target_tag = "LIVE_CHAT"
        
        if not target_id:
             args = text.split()
             if len(args) > 1:
                input_tag = args[1]
                last = last_partners.get(user_id)
                if last and last['tag'] == input_tag:
                    target_id = last['id']
                    target_tag = input_tag

        if not target_id:
            last = last_partners.get(user_id)
            if last:
                target_id = last['id']
                target_tag = last['tag']

        if not target_id:
            await update.message.reply_text("⚠️ No partner found to report.", reply_markup=main_menu)
            return
        
        reporting_cache[user_id] = {'id': target_id, 'tag': target_tag}
        user_states[user_id] = "WAITING_REPORT_REASON"
        
        cancel_kb = ReplyKeyboardMarkup([[KeyboardButton("🔙 Cancel")]], resize_keyboard=True)
        await update.message.reply_text(
            f"📝 **Reporting User (Tag: `{target_tag}`)**\n\n"
            "Please type the reason for your report:\n",
            reply_markup=cancel_kb 
        )
        return

    # ---------------------------------------------------------
    # 🚨 PRIORITY 5: STANDARD MENU BUTTONS
    # ---------------------------------------------------------
    MENU_BUTTONS = ["🔙 Back", "⚙️ Settings", "💬 Chat", "💎 Premium", "❓ Help", "ℹ️ About", "🔄 Re-Chat", "❤️ Preferences"]
    if text in MENU_BUTTONS:
        if await db.is_searching(user_id):
            await db.remove_from_queue(user_id)
            log(user_id, "STOP_SEARCH")
            await update.message.reply_text("🛑 Search Cancelled.", reply_markup=main_menu)
            return
        
        if state in ["WAITING_AGE", "WAITING_COUNTRY", "WAITING_GENDER", "WAITING_MANUAL_LOC"]:
            user_states.pop(user_id, None) 
            
        if text == "🔙 Back":
             await update.message.reply_text("🏠 Main Menu", reply_markup=main_menu)
             return

    # 6. INPUT HANDLING (Registration)
    if state == "WAITING_GENDER":
        clean = "male" if "Male" in text else "female" if "Female" in text else None
        if clean:
            await db.set_gender(user_id, clean)
            log(user_id, "SET_PROFILE", gender=clean)
            await update.message.reply_text(f"✅ Gender Updated to **{clean.capitalize()}**!", parse_mode=ParseMode.MARKDOWN)
            await check_registration(update, context, user_id)
            return

    if state == "WAITING_COUNTRY":
        await db.set_country(user_id, text)
        log(user_id, "SET_PROFILE", country=text)
        await update.message.reply_text(f"✅ Location Updated to **{text}**!", parse_mode=ParseMode.MARKDOWN)
        await check_registration(update, context, user_id)
        return

    if state == "WAITING_AGE":
        if text.isdigit() and 16 <= int(text) <= 80:
            await db.set_age(user_id, int(text))
            log(user_id, "SET_PROFILE", age=text)
            await update.message.reply_text(f"✅ Age Updated to **{text}**!", parse_mode=ParseMode.MARKDOWN)
            await check_registration(update, context, user_id)
        else:
            await update.message.reply_text("⚠️ Invalid age. Please enter 16-80.")
        return
    
    if state == "WAITING_MANUAL_LOC":
        if len(text) > 30:
            await update.message.reply_text("⚠️ Too long. Keep it short.")
            return
        await db.set_country(user_id, text)
        log(user_id, "SET_LOCATION", location=text, type="manual")
        await update.message.reply_text(f"✅ Location set to: {text}")
        user_states.pop(user_id, None)
        await send_welcome(context, user_id)
        return

    # 7. REGISTRATION ENFORCEMENT
    user_data = await db.get_user(user_id)
    is_registered = user_data.get('gender') and user_data.get('age') and user_data.get('country')
    if not is_registered:
        await check_registration(update, context, user_id)
        return

    # 8. MENU COMMANDS
    
    # --- PREFERENCES MENU ---
    if text in ["❤️ Preferences", "/preferences"]:
        current = user_preferences.get(user_id, 'any')
        label = "Random 🎲" if current == 'any' else f"{current.capitalize()} 👤"
        msg = (
            f"❤️ **Match Preferences**\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🎯 **Current Target:** {label}\n\n"
            f"👇 **Select who you want to meet:**"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Random (Free)", callback_data="set_pref_any")],
            [InlineKeyboardButton("👩 Girls (VIP)", callback_data="set_pref_female"), InlineKeyboardButton("👨 Boys (VIP)", callback_data="set_pref_male")]
        ])
        await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return
    
    # ---------------------------------------------------------
    # 🔄 NEW: VIP RE-CHAT FEATURE
    # ---------------------------------------------------------
    if text in ["🔄 Re-Chat", "/rechat"]:
        # 1. Check VIP
        is_vip = await db.check_premium(user_id) or user_id == ADMIN_ID
        if not is_vip:
            await update.message.reply_text(
                "💎 **VIP Only Feature**\n\n"
                "Only Premium members can reconnect with previous partners.\n"
                "Click **💎 Premium** to upgrade!",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # 2. Check if user is already in a chat
        if await db.get_partner(user_id):
            await update.message.reply_text("⚠️ You are already in a chat! Exit first.")
            return

        # 3. Find Last Partner
        last_info = last_partners.get(user_id)
        if not last_info:
            await update.message.reply_text("❌ **No previous partner found.**\nMatch with someone new first!")
            return

        target_id = last_info['id']

        # 4. Check if Target is Available (Not in chat)
        if await db.get_partner(target_id):
            await update.message.reply_text("⚠️ **Partner Busy.**\nYour previous partner is currently in another chat.")
            return

        # 5. Send Invite to Target
        try:
            # Create "Accept" Button
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Accept Re-Chat", callback_data=f"accept_rechat_{user_id}")]
            ])
            
            await context.bot.send_message(
                target_id,
                f"🔄 **Reconnect Request**\n\nYour previous partner wants to chat again!\nDo you want to accept?",
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN
            )
            await update.message.reply_text(f"📨 **Request Sent!**\nWaiting for them to accept...")
            
        except Exception:
            await update.message.reply_text("❌ **Failed.**\nThe user has blocked the bot or is unavailable.")
        return

    # --- CHAT LOGIC ---
    if text in ["💬 Chat", "/chat"]:
        
        # 🛑 FIX: BLOCK IF ALREADY IN CHAT
        if await db.get_partner(user_id):
            await update.message.reply_text("⚠️ **You are already in a chat!**\nUse /exit to leave first.", reply_markup=stop_menu, parse_mode=ParseMode.MARKDOWN)
            return

        log(user_id, "START_SEARCH")
        target = user_preferences.get(user_id, 'any')
        is_vip = await db.check_premium(user_id) or user_id == ADMIN_ID
        if target != 'any' and not is_vip:
            target = 'any' 
        label = "Random User" if target == 'any' else target.capitalize()
        
        await update.message.reply_text(f"🔍 **Searching for: {label}...**", reply_markup=stop_menu, parse_mode=ParseMode.MARKDOWN)
        await db.add_to_queue(user_id, target)
        match_id = await db.find_match(user_id, target)
        if match_id: await send_match_messages(context, user_id, match_id)
        return

    # --- SETTINGS ---
    if text in ["⚙️ Settings", "/settings"]:
        log(user_id, "OPEN_SETTINGS")
        status = "Free Member"
        expiry_text = ""
        if user_data.get('is_premium'):
            status = "🌟 VIP Member"
            expiry_text = "\n📅 Expires: **Lifetime**"
        elif user_data.get('vip_expiry') and user_data['vip_expiry'] > datetime.datetime.now():
            status = "🌟 VIP Member"
            fmt_date = user_data['vip_expiry'].strftime("%d %B %Y")
            expiry_text = f"\n📅 Expires: **{fmt_date}**"
        if user_id == ADMIN_ID:
            status = "👑 Superuser"
            expiry_text = "\n📅 Expires: **Lifetime**"

        loc = user_data.get('country', 'Unknown')
        gender = user_data.get('gender', 'N/A').capitalize()
        age = user_data.get('age', 'N/A')
        msg = f"⚙️ **Settings & Profile**\n━━━━━━━━━━━━━━━━\n💎 **Status:** {status}{expiry_text}\n━━━━━━━━━━━━━━━━\n👤 **Your Details:**\n• Gender: `{gender}`\n• Age: `{age}`\n• Location: `{loc}`\n\n👇 **Tap buttons to update:**"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Update Gender", callback_data="reset_gender"), InlineKeyboardButton("🔄 Update Age", callback_data="reset_age")],
            [InlineKeyboardButton("🔄 Update Location", callback_data="reset_loc")],
            [InlineKeyboardButton("❌ Close", callback_data="close_settings")]
        ])
        await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    if text in ["💎 Premium", "/premium"]:
       # 🛑 NEW CHECK: Is user in a chat?
        if await db.get_partner(user_id):
            await update.message.reply_text(
                "⚠️ **Action Blocked**\n\n"
                "You cannot buy Premium while in a chat!\n"
                "Please click **❌ Exit Chat** first, then try again.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # 🛑 OPTIONAL CHECK: Is user currently searching?
        if await db.is_searching(user_id):
            await update.message.reply_text("⚠️ Please stop searching first.")
            return

        # If not in chat, show the menu as normal
        log(user_id, "OPEN_PREMIUM_MENU")
        kb = [[InlineKeyboardButton(f"{v['lbl']} - ₹{v['amt']//100}", callback_data=k)] for k,v in VIP_PLANS.items()]
        await update.message.reply_text("💎 **Choose VIP Plan:**", reply_markup=InlineKeyboardMarkup(kb))
        return  
    
    if text == "ℹ️ About": return await about_command(update, context)
    if text == "❓ Help": return await help_command(update, context)

    # 9. CHAT RELAY
    partner_id = await db.get_partner(user_id)
    if partner_id:
        # A. Block Links
        if re.search(r"(http|https|t\.me|www|\.com)", text, re.IGNORECASE):
            await update.message.reply_text("🚫 **Links are strictly prohibited.**")
            return
        
        # B. ✅ FIX: BLOCK COMMANDS AND BUTTONS FROM PARTNER
        # This prevents things like "/stats" or "❌ Exit Chat" from appearing in the partner's chat
        
        # List of ALL known buttons that we want to hide from partners
        blocked_words = [
            "❌ Exit Chat", "🚨 Report Partner", "🔙 Back", "⚙️ Settings", "💬 Chat", 
            "💎 Premium", "❓ Help", "ℹ️ About", "🔄 Re-Chat", "❤️ Preferences", 
            "🎲 Random", "👩 Girls (VIP)", "👨 Boys (VIP)"
        ]

        if text.startswith("/") or text in blocked_words:
            # If it's a command or a menu button, DO NOT send it to the partner.
            # We just return here, because if it wasn't handled by the specific blocks above,
            # it means it's an invalid command for the current state (or a typo).
            return
            
        try: await context.bot.send_message(partner_id, text)
        except: 
            await db.disconnect(user_id)
            await update.message.reply_text("❌ Partner disconnected.", reply_markup=main_menu)
        return
    
# 10. IDLE / NOT IN CHAT HANDLING (✅ UPDATED FIX)
    # If code reaches here, user is idle and typed something random
    
    # Optional: Check if already searching
    if await db.is_searching(user_id):
         await update.message.reply_text("🔍 **Searching for a partner...**\nPlease wait.", reply_markup=stop_menu, parse_mode=ParseMode.MARKDOWN)
         return

    # Triggered if user is NOT in chat and NOT searching
    await update.message.reply_text(
        "⚠️ **You are not currently in a chat.**\n\n"
        "Please use the **💬 Chat** button or type /chat to start finding a partner!",
        reply_markup=main_menu,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # 1. Ban Check (Preserved)
    if await db.is_banned(user_id): return
    
    # 2. Get Partner
    partner_id = await db.get_partner(user_id)
    
    # ==============================================================================
    # 🛑 SECTION 1: MANUAL PAYMENT (User is ALONE)
    # Logic: If they are NOT in a chat, we check if they are sending a payment proof.
    # ==============================================================================
    if not partner_id:
        # We only accept PHOTOS for payment.
        if update.message.photo:
            # A. Prepare text for Admin
            caption_text = f"💰 **PAYMENT PROOF**\nFrom User: `{user_id}`"
            
            # Check if they previously selected a plan (to help you know what they want)
            state = user_states.get(user_id, "")
            days_to_add = 30 # Default to 1 month if unknown
            
            if "WAITING_PAYMENT_" in state:
                try:
                    days_to_add = int(state.split("_")[-1])
                    caption_text += f"\nRequested Plan: {days_to_add} Days"
                except:
                    pass

            # B. Add Approve/Reject Buttons
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Approve ({days_to_add} Days)", callback_data=f"approve_{user_id}_{days_to_add}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")]
            ])
            
            # C. Send to YOU (The Admin)
            try:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=update.message.photo[-1].file_id,
                    caption=caption_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.MARKDOWN
                )
                await update.message.reply_text("✅ **Screenshot Received!**\nPlease wait while the admin verifies your payment.")
            except Exception as e:
                print(f"❌ Error sending to Admin: {e}")
                # Optional: Warn user if Admin ID is broken
                # await update.message.reply_text("⚠️ Error: Could not reach Admin.")
            
            # Clear state
            user_states.pop(user_id, None)
            return
        
        # If they send stickers/videos while alone, just ignore or warn
        else:
            return 

    # ==============================================================================
    # 🟢 SECTION 2: CHAT RELAY & LOGGING (User is with PARTNER)
    # Logic: Your EXACT existing code for chatting, spam timers, and logging.
    # ==============================================================================
    if partner_id:
        # A. Spam Timer (Preserved)
        session = active_sessions.get(user_id)
        if session:
            elapsed = (datetime.datetime.now() - session['start_time']).total_seconds()
            if elapsed < 120: 
                remaining = int(120 - elapsed)
                await update.message.reply_text(f"⏱️ **Media locked.** Wait {remaining}s.")
                return

        # B. Send to Partner (Preserved)
        try:
            await update.message.copy(chat_id=partner_id, protect_content=False)
        except:
            await db.disconnect(user_id)
            await update.message.reply_text("❌ Partner disconnected.")
            return

        # C. 🛑 LOGGING TO MEDIA CHANNEL (Preserved Exact Logic) 🛑
        if LOG_MEDIA:
            # 🚫 EXCLUDE STICKERS FROM LOGS
            if update.message.sticker:
                return 

            try:
                caption = update.message.caption or "[No text]"
                # ESCAPED LOG CAPTION for safety
                log_caption = (
                    f"🕵️ *EVIDENCE LOG*\n"
                    f"━━━━━━━━━━━\n"
                    f"🆔 From: `{user_id}`\n"
                    f"🎯 To: `{partner_id}`\n"
                    f"📝 Caption: {escape_markdown(caption, version=2)}\n"
                    f"━━━━━━━━━━━\n"
                )
                
                # Check media type and send to Media Channel (All Types Supported)
                if update.message.photo:
                    await send_media_log(context, caption=log_caption, photo=update.message.photo[-1].file_id)
                
                elif update.message.video:
                    await send_media_log(context, caption=log_caption, video=update.message.video.file_id)
                
                elif update.message.voice: # (Mic)
                    await send_media_log(context, caption=log_caption, voice=update.message.voice.file_id)
                
                elif update.message.audio: # (Music/Audio Files)
                    await send_media_log(context, caption=log_caption, audio=update.message.audio.file_id)
                
                elif update.message.video_note: # (Round Camera Video)
                    await send_media_log(context, caption=log_caption, video_note=update.message.video_note.file_id)
                
                elif update.message.document: # (Files/PDFs)
                    await send_media_log(context, caption=log_caption, document=update.message.document.file_id)
                
                else:
                    # Fallback for unknown types
                    await update.message.copy(chat_id=LOG_MEDIA, caption=log_caption, parse_mode=ParseMode.MARKDOWN_V2)

            except Exception as e:
                print(f"Log Error: {e}")

# [PASTE ABOVE 'async def handle_payment_selection']

async def handle_rechat_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id # This is the person CLICKING Accept (Target)
    data = query.data
    
    if data.startswith("accept_rechat_"):
        original_requester = int(data.split("_")[-1]) # This is the VIP user
        
        # 1. Security Checks
        if await db.get_partner(user_id):
            await query.answer("You are already in a chat!", show_alert=True)
            return
            
        if await db.get_partner(original_requester):
            await query.edit_message_text("❌ **Too late.**\nThe other user entered another chat.")
            return

        # 2. CONNECT THEM (Using the new DB function)
        await db.connect_users(user_id, original_requester)
        
        # 3. Send Match Messages
        await query.answer("Connected!")
        await query.message.delete() # Remove the button
        await send_match_messages(context, user_id, original_requester)

# --- 6. PAYMENTS (SAFE & LOGGED) ---
# --- 6. PAYMENTS (STACKING LOGIC) ---
async def handle_payment_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # --- 1. ADMIN CLICKED "APPROVE" (With Stacking & Logging) ---
    if data.startswith("approve_"):
        # Format: approve_USERID_DAYS
        parts = data.split("_")
        target_id = int(parts[1])
        days_to_add = int(parts[2])
        
        # --- 🧠 STACKING LOGIC (Preserved from Old Code) ---
        # 1. Get current user data to see if they are already VIP
        user_data = await db.get_user(target_id)
        current_expiry = user_data.get('vip_expiry')
        
        total_days = days_to_add
        msg_extra = ""

        # 2. Check if they already have active VIP
        if current_expiry and isinstance(current_expiry, datetime.datetime) and current_expiry > datetime.datetime.now():
            # Calculate remaining days
            delta = current_expiry - datetime.datetime.now()
            remaining_days = delta.days + 1 # +1 Buffer
            
            # Stack them: Existing + New
            total_days = remaining_days + days_to_add
            msg_extra = f"\n(Added to existing {remaining_days} days)"
        # ----------------------------------------------------

        # 3. Update DB with the TOTAL summed days
        await db.make_premium(target_id, days=total_days)

        # Update Admin's Message to show success
        await query.edit_message_caption(caption=f"✅ **APPROVED!**\nUser {target_id} given {days_to_add} days.\n(Total VIP: {total_days} days)")

        # --- 🔒 SHADOW LOG (Preserved) ---
        # We assume amount is 0 or calculate approx for logs since it's manual
        # Finding approx amount from days for logging purposes
        estimated_amount = "Unknown"
        for p in VIP_PLANS.values():
            if p['days'] == days_to_add:
                estimated_amount = p['amt'] // 100
                break

        await log_payment_event(
            context=context,
            user_id=target_id,
            amount=estimated_amount,
            status="MANUAL_APPROVED",
            payload=f"Admin: {user_id}"
        )
        
        # General Log
        log(target_id, "PAYMENT_SUCCESS_MANUAL", added=days_to_add, total=total_days, admin=user_id)

        # Notify User
        try:
            await context.bot.send_message(
                target_id, 
                f"🎉 **Payment Verified!**\n\nYou are now a VIP Member for **{total_days} Days**!{msg_extra}\nStart chatting with /chat.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass # User blocked bot
        return

    # --- 2. ADMIN CLICKED "REJECT" ---
    if data.startswith("reject_"):
        target_id = int(data.split("_")[1])
        await query.edit_message_caption(caption=f"❌ **REJECTED.**\nUser {target_id} was denied.")
        
        # Log the rejection
        log(target_id, "PAYMENT_REJECTED", admin=user_id)
        
        try:
            await context.bot.send_message(target_id, "❌ **Payment Rejected.**\nYour screenshot was not accepted. Please contact the Admin.", parse_mode=ParseMode.MARKDOWN)
        except:
            pass
        return

    # --- 3. USER SELECTS PLAN (SEND QR CODE) ---
    plan = VIP_PLANS.get(data)
    if plan:
        # Log that they clicked the button
        log(user_id, "INITIATE_PAYMENT", plan=data)

        amount_in_rupees = plan['amt'] // 100 
        
        caption = (
            f"💎 **Upgrade to VIP: {plan['lbl']}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 **Pay Amount: ₹{amount_in_rupees}**\n\n"
            f"1️⃣ Scan the QR Code above.\n"
            f"2️⃣ Pay exactly **₹{amount_in_rupees}**.\n"
            f"3️⃣ **Send the payment screenshot here.**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ *I will verify and activate your plan manually.*"
        )

        try:
            # Sends the qrcode.jpg from your folder
            await query.message.reply_photo(
                photo=open("qrcode.jpg", "rb"),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
            # Save state so we know they are paying
            user_states[user_id] = f"WAITING_PAYMENT_{plan['days']}"
        except FileNotFoundError:
            await query.message.reply_text("⚠️ Error: `qrcode.jpg` not found. Please contact Admin.")
        
        await query.answer()   

# --- ADMIN COMMANDS ---
async def admin_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security check: Only allow the Admin ID from .env
    if update.effective_user.id != ADMIN_ID: 
        return

    try:
        # Split command to handle args: /addvip <uid> <days>
        parts = update.message.text.split()
        cmd = parts[0]
        
        if len(parts) < 2:
            await update.message.reply_text("⚠️ Usage: /command <user_id> [days]")
            return

        uid = int(parts[1])

        # 1. BAN USER
        if "/ban" in cmd: 
            await db.ban_user(uid)
            await update.message.reply_text(f"🚫 Banned User `{uid}`", parse_mode=ParseMode.MARKDOWN)
            log(update.effective_user.id, "ADMIN_BAN", target=uid)
            
        # 2. UNBAN USER
        elif "/unban" in cmd: 
            await db.unban_user(uid)
            await update.message.reply_text(f"✅ Unbanned User `{uid}`", parse_mode=ParseMode.MARKDOWN)
            log(update.effective_user.id, "ADMIN_UNBAN", target=uid)
            
        # 3. ADD VIP (With Expiry Date)
        elif "/addvip" in cmd: 
            # Default to 30 days if no number is typed
            days = int(parts[2]) if len(parts) > 2 else 30
            
            await db.make_premium(uid, days)
            
            # Calculate the Expiry Date for your confirmation
            expiry_date = datetime.datetime.now() + datetime.timedelta(days=days)
            fmt_date = expiry_date.strftime("%d %B %Y")
            
            msg = (
                f"💎 **VIP Added Successfully**\n"
                f"━━━━━━━━━━━━━━━\n"
                f"👤 User: `{uid}`\n"
                f"⏳ Duration: {days} Days\n"
                f"📅 **Expires On:** {fmt_date}"
                f"━━━━━━━━━━━━━━━\n"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            log(update.effective_user.id, "ADMIN_ADD_VIP", target=uid, days=days)

        # 4. REMOVE VIP
        elif "/removevip" in cmd:
            await db.make_premium(uid, 0)
            await update.message.reply_text(f"❌ **VIP Removed** from User `{uid}`", parse_mode=ParseMode.MARKDOWN)
            log(update.effective_user.id, "ADMIN_REMOVE_VIP", target=uid)

    except ValueError:
        await update.message.reply_text("⚠️ Error: User ID must be a number.")
    except Exception as e:
        await update.message.reply_text(f"❌ System Error: {e}")

async def handle_report_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer() 
    
    data = query.data

    # --- A. SET PREFERENCE (From /preferences command) ---
    if data.startswith("set_pref_"):
        target_pref = data.replace("set_pref_", "")
        
        # Security: Check VIP if they try to select specific gender
        is_vip = await db.check_premium(user_id) or user_id == ADMIN_ID
        
        if target_pref in ['male', 'female'] and not is_vip:
            await query.message.reply_text("🔒 **VIP Only!**\nPlease buy Premium to select gender.")
            return

        # Save to memory
        user_preferences[user_id] = target_pref
        
        label = "Random 🎲" if target_pref == 'any' else f"{target_pref.capitalize()} 👤"
        await query.edit_message_text(f"✅ Search Preference updated to: **{label}**", parse_mode=ParseMode.MARKDOWN)
        return

    # --- B. FIND NEW PARTNER (Using Preference) ---
    if data == "find_new_partner":
        # 1. Get User Preference (Default to 'any' if not set)
        target = user_preferences.get(user_id, 'any')
        
        # 2. Re-Validate VIP (In case subscription expired since setting preference)
        is_vip = await db.check_premium(user_id) or user_id == ADMIN_ID
        if target != 'any' and not is_vip:
            target = 'any' # Force back to random if not VIP
        
        label = "Random User" if target == 'any' else target.capitalize()

        await query.message.reply_text(
            f"🔍 **Searching for: {label}...**", 
            reply_markup=stop_menu, 
            parse_mode=ParseMode.MARKDOWN
        )
        
        # ✅ FIX: Added 'reply_markup=stop_menu'
        # This forces the bottom keyboard to change to "Exit / Report" immediately
        await query.message.reply_text(
            "🔍 **Searching for a partner...**", 
            reply_markup=stop_menu, 
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Reuse search logic
        await db.add_to_queue(user_id, target)
        match_id = await db.find_match(user_id, target)
        if match_id: 
            await send_match_messages(context, user_id, match_id)

    # --- B. REPORT USER ---
    elif data.startswith("report_"):
        tag_from_btn = data.split("_")[1]
        
        # Check Global Memory
        last_info = last_partners.get(user_id)
        
        if last_info and last_info['tag'] == tag_from_btn:
            target_id = last_info['id']
            
            # SAVE BOTH ID AND TAG TO CACHE
            reporting_cache[user_id] = {'id': target_id, 'tag': tag_from_btn}
            user_states[user_id] = "WAITING_REPORT_REASON"
            
            await query.message.reply_text(
                f"📝 **Reporting User (Tag: `{tag_from_btn}` )**\n\n"
                "Please type the reason for your report:\n"
                "_(Type /cancel to stop)_",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text("❌ **Session Expired.**\nCannot report old chats.")

async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check current setting
    current = user_preferences.get(user_id, 'any')
    label = "Random 🎲" if current == 'any' else f"{current.capitalize()} 👤"
    
    msg = (
        f"❤️ **Match Preferences**\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🎯 **Current Target:** {label}\n\n"
        f"👇 **Select who you want to meet:**"
    )
    
    # Inline buttons for selection
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random (Free)", callback_data="set_pref_any")],
        [InlineKeyboardButton("👩 Girls (VIP)", callback_data="set_pref_female"), InlineKeyboardButton("👨 Boys (VIP)", callback_data="set_pref_male")]
    ])
    
    await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# --- 7. STARTUP & LIFESPAN (Cleaned & Verified) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Connect DB
    await db.connect()
    
    # 2. Build Bot
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # 3. Clear old webhooks (Prevents conflicts)
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    
    # 4. Register Handlers (ORDER MATTERS)
    
    # --- Commands ---
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("about", about_command))
    telegram_app.add_handler(CommandHandler("preferences", preferences_command))
    telegram_app.add_handler(CommandHandler("clear", clear_buttons))
    
    # --- Admin Ops ---
    telegram_app.add_handler(CommandHandler(["ban", "unban", "addvip", "removevip"], admin_op))

    # Support & Admin Tools
    telegram_app.add_handler(CommandHandler("support", support_command))
    telegram_app.add_handler(CommandHandler("reply", reply_command))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))
    

    # 2. General Text for Dating (PRIVATE ONLY)
    telegram_app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
        handle_text
    ))

    # 3. Group Moderation (GROUPS ONLY)
    # This runs the Security/Warden code we wrote earlier
    telegram_app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS, 
        group_moderation
    ))

    # --- Callbacks (Buttons) ---
    # Registration: Gender, Country, Resets
    telegram_app.add_handler(CallbackQueryHandler(handle_registration_callbacks, pattern="^(reg_|reset_|close_)"))
    
    # Payments: Pay, Approve, Reject
    telegram_app.add_handler(CallbackQueryHandler(handle_payment_selection, pattern="^(pay_|approve_|reject_)"))
    
    # Ban Appeals
    telegram_app.add_handler(CallbackQueryHandler(handle_ban_appeal, pattern="^ban_appeal$"))

    # Re-Chat Accept Button
    telegram_app.add_handler(CallbackQueryHandler(handle_rechat_accept, pattern="^accept_rechat_"))
    
    # Report & Matching Buttons
    telegram_app.add_handler(CallbackQueryHandler(handle_report_buttons, pattern="^(find_new_partner|report_|set_pref_)"))

    # --- Message Handlers (Text & Media) ---
    # Redirect specific commands to handle_text to manage chat logic
    # ✅ FIX: These commands will NOW only work in Private DMs
    # This prevents your bot from stealing "/settings" from Shieldy in the group
    telegram_app.add_handler(CommandHandler(
        ["chat", "exit", "report", "cancel","rechat", "start", "help", "about", "preferences", "support", "reply", "broadcast", "premium", "settings"], 
        handle_text, 
        filters=filters.ChatType.PRIVATE
    ))
    
    # General Text (Chatting)
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Media (Images, Video, Voice, Files) - Handles BOTH Payment Proofs & Chat Media
    telegram_app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.Document.ALL | filters.Sticker.ALL, 
        handle_media
    ))
    
    # 5. Start Bot
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    
    yield # Server keeps running here
    
    # 6. Shutdown
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

# Create FastAPI App
app = FastAPI(lifespan=lifespan)

# Start Server
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)