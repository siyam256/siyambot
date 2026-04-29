import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
import os
import pandas as pd
import asyncio
import nest_asyncio
import json
import google.generativeai as genai
from functools import wraps

# Apply the patch to allow nested event loops
nest_asyncio.apply()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, BaseHandler, CallbackQueryHandler

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "7956624694:AAE6e9UwkT2im8iIwslvxnxRZuy7EfFBz6A"
GEMINI_API_KEY = "AIzaSyB92WWNHpjUun4AZ2BZC9Ea3IgPM8VAnUU"

# --- USER & SETTINGS CONFIG ---
ADMIN_ID = 7145991193
AUTHORIZED_USERS_FILE = "authorized_users.json"
SETTINGS_FILE = "settings.json"
authorized_users = set()
AI_EXPLANATION_ENABLED = True # Default is ON

# --- SETUP ---
# Configure Gemini API
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

# Configure Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
poll_data_storage = {}

# --- DATA PERSISTENCE FUNCTIONS ---
def load_data():
    global authorized_users, AI_EXPLANATION_ENABLED
    try:
        with open(AUTHORIZED_USERS_FILE, 'r') as f:
            authorized_users = set(json.load(f))
    except FileNotFoundError:
        authorized_users = set()
    try:
        with open(SETTINGS_FILE, 'r') as f:
            AI_EXPLANATION_ENABLED = json.load(f).get('ai_explanation_enabled', True)
    except FileNotFoundError:
        AI_EXPLANATION_ENABLED = True

def save_authorized_users():
    with open(AUTHORIZED_USERS_FILE, 'w') as f:
        json.dump(list(authorized_users), f)

def save_settings():
    with open(SETTINGS_FILE, 'w') as f:
        json.dump({'ai_explanation_enabled': AI_EXPLANATION_ENABLED}, f)

# --- DECORATORS FOR ACCESS CONTROL ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context, *args, **kwargs):
        user = update.effective_user
        if not user or (user.id != ADMIN_ID and user.id not in authorized_users):
            if update.message:
                await update.message.reply_text("⛔ দুঃখিত, এই বটটি ব্যবহার করার অনুমতি আপনার নেই।")
            elif update.callback_query:
                await update.callback_query.answer("⛔ অনুমতি নেই।", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context, *args, **kwargs):
        if update.effective_user.id == ADMIN_ID:
            return await func(update, context, *args, **kwargs)
        else:
            await update.message.reply_text("⛔ শুধুমাত্র অ্যাডমিন এই কমান্ডটি ব্যবহার করতে পারবেন।")
    return wrapped

# --- BOT HANDLERS ---
@restricted
async def start_csv(update: Update, context) -> None:
    user_id = update.effective_user.id
    poll_data_storage[user_id] = []
    await update.message.reply_text('CSV generation process started. Forward polls to me.')

@restricted
async def done_csv(update: Update, context) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("⚠️ অনুগ্রহ করে একটি ফাইলের নাম দিন। যেমন: `/done_csv biology_chapter_1`")
        return
    filename = context.args[0]
    csv_file_path = f'{filename}.csv'
    if user_id in poll_data_storage and poll_data_storage[user_id]:
        try:
            df = pd.DataFrame(poll_data_storage[user_id])
            df.to_csv(csv_file_path, index=False)
            with open(csv_file_path, 'rb') as f:
                await update.message.reply_document(document=f, filename=csv_file_path)
        finally:
            poll_data_storage[user_id] = [] # Counter resets here
            if os.path.exists(csv_file_path): os.remove(csv_file_path)
    else:
        await update.message.reply_text('No poll data found.')

@restricted
async def handle_poll(update: Update, context) -> None:
    user_id = update.effective_user.id
    if user_id not in poll_data_storage:
        await start_csv(update, context)
    poll = update.effective_message.poll
    if not poll or not poll.is_anonymous: return

    # Store poll data first
    poll_entry = {
        'questions': poll.question,
        'option1': poll.options[0].text if len(poll.options) > 0 else '',
        'option2': poll.options[1].text if len(poll.options) > 1 else '',
        'option3': poll.options[2].text if len(poll.options) > 2 else '',
        'option4': poll.options[3].text if len(poll.options) > 3 else '',
        'option5': poll.options[4].text if len(poll.options) > 4 else '',
        'answer': poll.correct_option_id + 1 if poll.correct_option_id is not None else '',
        'explanation': "", # Placeholder
        'type': 1, 'section': 1
    }

    explanation_text = ""
    if AI_EXPLANATION_ENABLED:
        status_message = await update.message.reply_text("Poll received. Generating AI explanation... ⌛")
        explanation_text = await generate_explanation(poll.question, [opt.text for opt in poll.options], poll.correct_option_id)
        if explanation_text.startswith("Sorry,"):
            await status_message.edit_text(f"⚠️ {explanation_text}")
            explanation_text = "" # Store blank if error
        else:
            # Edit success message later
            pass
    else:
        explanation_text = poll.explanation if poll.explanation else ""

    poll_entry['explanation'] = explanation_text
    poll_data_storage[user_id].append(poll_entry)

    # Get the new count
    poll_count = len(poll_data_storage[user_id])

    # Now send the final confirmation message
    if AI_EXPLANATION_ENABLED and not explanation_text.startswith("Sorry,"):
        await status_message.edit_text(f"✅ Poll #{poll_count} and AI explanation stored successfully.")
    else:
        await update.message.reply_text(f"✅ Poll #{poll_count} stored successfully (AI explanations are OFF).")

async def generate_explanation(question: str, options: list[str], correct_option_id: int) -> str:
    if correct_option_id is None: return "Sorry, this is a quiz-style poll without a correct answer."

    prompt = f"""You are an expert tutor for Bangladeshi HSC syllabus. Provide a beautiful, informative, and concise explanation for the following question.
**Strict Rules:**
1. The entire explanation **MUST** be under 200 characters.
2. Use proper notation for math and science (e.g., H₂O, SO₄²⁻, x²).
3. If it's a math problem, solve it step-by-step.

Question: {question}
Options: {', '.join(options)}
Correct Answer: {options[correct_option_id]}

Explanation:"""

    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt
        )
        if not response.parts:
            logger.warning(f"Gemini content blocked. Feedback: {response.prompt_feedback}")
            return "Sorry, the explanation was blocked by safety filters."

        explanation = response.text.strip()
        return explanation[:200]
    except Exception as e:
        logger.error(f"An unexpected error occurred with Gemini SDK: {e}")
        return "Sorry, an AI error occurred."

# --- SETTINGS & ADMIN HANDLERS ---
@restricted
async def show_settings(update: Update, context) -> None:
    status = "ON 🟢" if AI_EXPLANATION_ENABLED else "OFF 🔴"
    button_text = "Turn OFF 🔴" if AI_EXPLANATION_ENABLED else "Turn ON 🟢"
    callback_data = "toggle_ai_off" if AI_EXPLANATION_ENABLED else "toggle_ai_on"

    keyboard = [[InlineKeyboardButton(button_text, callback_data=callback_data)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(f"AI Explanation feature is currently **{status}**.", reply_markup=reply_markup, parse_mode='Markdown')

@restricted
async def button_callback(update: Update, context) -> None:
    global AI_EXPLANATION_ENABLED
    query = update.callback_query
    await query.answer()

    if query.data == 'toggle_ai_on': AI_EXPLANATION_ENABLED = True
    elif query.data == 'toggle_ai_off': AI_EXPLANATION_ENABLED = False

    save_settings()

    status = "ON 🟢" if AI_EXPLANATION_ENABLED else "OFF 🔴"
    button_text = "Turn OFF 🔴" if AI_EXPLANATION_ENABLED else "Turn ON 🟢"
    callback_data = "toggle_ai_off" if AI_EXPLANATION_ENABLED else "toggle_ai_on"

    keyboard = [[InlineKeyboardButton(button_text, callback_data=callback_data)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text=f"AI Explanation feature is now **{status}**.", reply_markup=reply_markup, parse_mode='Markdown')

@admin_only
async def add_user(update: Update, context) -> None:
    if not context.args: await update.message.reply_text("⚠️ ব্যবহার: `/adduser <user_id>`"); return
    try:
        user_id = int(context.args[0])
        if user_id in authorized_users: await update.message.reply_text(f"User ID {user_id} আগে থেকেই তালিকায় রয়েছে।"); return
        authorized_users.add(user_id)
        save_authorized_users()
        await update.message.reply_text(f"✅ User ID {user_id} সফলভাবে যোগ করা হয়েছে।")
    except ValueError: await update.message.reply_text("❌ ভুল ইউজার আইডি। শুধুমাত্র সংখ্যা ব্যবহার করুন।")

@admin_only
async def del_user(update: Update, context) -> None:
    if not context.args: await update.message.reply_text("⚠️ ব্যবহার: `/deluser <user_id>`"); return
    try:
        user_id = int(context.args[0])
        if user_id in authorized_users:
            authorized_users.remove(user_id)
            save_authorized_users()
            await update.message.reply_text(f"🗑️ User ID {user_id} তালিকা থেকে حذف করা হয়েছে।")
        else: await update.message.reply_text(f"User ID {user_id} তালিকায় পাওয়া যায়নি।")
    except ValueError: await update.message.reply_text("❌ ভুল ইউজার আইডি। শুধুমাত্র সংখ্যা ব্যবহার করুন।")

@admin_only
async def list_users(update: Update, context) -> None:
    if not authorized_users: await update.message.reply_text("অনুমতিপ্রাপ্ত ব্যবহারকারীর তালিকা বর্তমানে খালি।"); return
    user_list = "📜 **অনুমতিপ্রাপ্ত ব্যবহারকারীর তালিকা:**\n" + "\n".join(f"- `{user_id}`" for user_id in authorized_users)
    await update.message.reply_text(user_list, parse_mode='Markdown')

async def error_handler(update: object, context) -> None:
    logger.warning('Update "%s" caused error "%s"', update, context.error)

# --- BOT STARTUP LOGIC ---
async def main() -> None:
    load_data()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_csv))
    application.add_handler(CommandHandler("start_csv", start_csv))
    application.add_handler(CommandHandler("done_csv", done_csv))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.POLL, handle_poll))
    application.add_handler(CommandHandler("adduser", add_user))
    application.add_handler(CommandHandler("deluser", del_user))
    application.add_handler(CommandHandler("listusers", list_users))
    application.error_handler = error_handler

    print("Starting bot...")
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("Bot is running. Stop the cell to shut it down.")
        await asyncio.Future()
    except: pass
    finally:
        print("Shutting down bot...")

if __name__ == '__main__':
    asyncio.run(main())
