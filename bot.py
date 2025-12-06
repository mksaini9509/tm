import os
import asyncio
import logging
import sqlite3
import sys 
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# --- कॉन्फ़िगरेशन और सेटअप ---
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
SUPER_ADMIN_ID_STR = os.getenv('SUPER_ADMIN_ID')
SUPER_ADMIN_ID = None

if not BOT_TOKEN:
    print("FATAL ERROR: .env में BOT_TOKEN नहीं मिला!")
    sys.exit(1)

try:
    if SUPER_ADMIN_ID_STR:
        SUPER_ADMIN_ID = int(SUPER_ADMIN_ID_STR)
except ValueError:
    logging.error("FATAL ERROR: SUPER_ADMIN_ID को संख्या (int) में बदला नहीं जा सका।")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Conversation States ---
SELECTING_CHANNEL, AWAITING_QUIZ_INPUT, AWAITING_CHANNEL_INFO, AWAITING_ADMIN_ID = range(4)

DATABASE_NAME = 'quiz_bot.db'
DELETE_DELAY_SECONDS = 5
TELEGRAM_RATE_LIMIT_DELAY = 1.5

# --- डेटाबेस फ़ंक्शंस ---

def setup_db():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS channels (chat_id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def manage_admin(user_id, action='add'):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if action == 'add':
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    elif action == 'remove':
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    was_successful = cursor.rowcount > 0
    conn.close()
    return was_successful

def get_channels():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, name FROM channels")
    channels = cursor.fetchall()
    conn.close()
    return channels

def manage_channel(chat_id, name=None, action='add'):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if action == 'add' and name:
        cursor.execute("INSERT OR IGNORE INTO channels (chat_id, name) VALUES (?, ?)", (str(chat_id), name))
    elif action == 'remove':
        cursor.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
    conn.commit()
    was_successful = cursor.rowcount > 0
    conn.close()
    return was_successful

def get_all_admins():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins")
    admins = [row[0] for row in cursor.fetchall()]
    conn.close()
    return admins

# --- यूटिलिटी फ़ंक्शंस और चेक्स ---

async def check_auth(update: Update):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.callback_query:
            message = update.callback_query.message
        else:
            message = update.message
        
        await message.reply_text("⛔ आपको यह कमांड चलाने की अनुमति नहीं है।")
        return False
    return True

async def delete_message_after_delay(message: Update.message, delay=DELETE_DELAY_SECONDS):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

# --- कमांड और हैंडलर्स ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_admin(user_id):
        msg = await update.message.reply_text(
            "🎉 स्वागत है! आप एडमिन हैं।\n"
            "क्विज़ शुरू करने के लिए `/quiz` या कंट्रोल पैनल के लिए `/admin` चलाएं।"
        )
    else:
        msg = await update.message.reply_text("👋 नमस्कार! यह एक प्राइवेट क्विज़ क्रिएशन बॉट है।")
    
    await delete_message_after_delay(msg)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एडमिन कंट्रोल पैनल दिखाता है"""
    if not await check_auth(update):
        return

    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        # पुराने मैसेज को हटाने की कोशिश करें (ताकि Edit Error न आए)
        try:
             await message.delete()
        except Exception:
             pass
    else:
        message = update.message

    keyboard = [
        [InlineKeyboardButton("➕ नया चैनल जोड़ें", callback_data='admin_add_channel'),
         InlineKeyboardButton("🗑️ चैनल डिलीट करें", callback_data='admin_delete_channel')],
        [InlineKeyboardButton("👥 एडमिन मैनेज करें", callback_data='admin_manage_admins')],
        [InlineKeyboardButton("✅ क्विज शुरू करें", callback_data='start_quiz_flow')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # हमेशा नया मैसेज भेजें ताकि Bad Request error न आए
    await message.reply_text('⚙️ एडमिन कंट्रोल पैनल:', reply_markup=reply_markup)

    return ConversationHandler.END


# --- QUIZ CONVERSATION FLOW ---

async def start_quiz_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quiz Conversation शुरू करता है और चैनल सिलेक्शन दिखाता है"""
    
    if not await check_auth(update):
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
    else:
        message = update.message

    channels = get_channels()
    if not channels:
        await message.reply_text("❌ कोई चैनल जोड़ा नहीं गया है। कृपया `/admin` से चैनल जोड़ें।")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=f'select_channel_{chat_id}')] for chat_id, name in channels]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # नया मैसेज भेजें
    await message.reply_text('🎯 क्विज भेजने के लिए चैनल चुनें:', reply_markup=reply_markup)

    return SELECTING_CHANNEL

async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """चयनित चैनल ID को स्टोर करता है और अगले स्टेप पर जाता है"""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.data.replace('select_channel_', '')
    context.user_data['target_chat_id'] = chat_id
    
    # नया मैसेज भेजें
    await query.message.reply_text(
        f'✅ चैनल `{chat_id}` चुना गया।\n\n'
        'अब, **कॉमा-सेपरेटेड** फॉर्मेट में अपने क्विज़ मैसेज भेजें:\n'
        '`प्रश्न,ऑप्शन1,ऑप्शन2,ऑप्शन3,ऑप्शन4,सही_उत्तर(1-4),एक्सप्लेनेशन`'
    , parse_mode='Markdown')
    
    return AWAITING_QUIZ_INPUT

async def process_quiz_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """क्विज़ टेक्स्ट को प्रोसेस करता है"""
    
    target_chat_id = context.user_data.get('target_chat_id')
    if not target_chat_id:
        await update.message.reply_text("❌ त्रुटि: कोई चैनल नहीं चुना गया। कृपया `/quiz` से दोबारा शुरू करें।")
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    raw_text = update.message.text
    lines = raw_text.strip().split('\n')
    
    success_count = 0
    error_count = 0
    
    status_msg = await update.message.reply_text(f"⏳ {len(lines)} क्विज़ `{target_chat_id}` में प्रोसेस हो रहे हैं...")

    for line in lines:
        if not line.strip():
            continue
            
        try:
            parts = [p.strip() for p in line.split(',')]
            
            if len(parts) < 6:
                raise ValueError("Format Error: कम से कम 6 चीजें (प्रश्न, 4 ऑप्शन, उत्तर) होनी चाहिए")

            question = parts[0]
            options = parts[1:5]
            correct_id = int(parts[5]) - 1 
            
            expl = parts[6] if len(parts) >= 7 and parts[6].strip() else "Join-@mk_study_hub"

            await context.bot.send_poll(
                chat_id=target_chat_id,
                question=question,
                options=options,
                type='quiz',
                correct_option_id=correct_id,
                explanation=expl,
                is_anonymous=True
            )
            success_count += 1
            await asyncio.sleep(TELEGRAM_RATE_LIMIT_DELAY) 

        except Exception as e:
            error_count += 1
            if "Bad Request" in str(e):
                 await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=f"API Error! Quiz failed in `{target_chat_id}`. सुनिश्चित करें कि बॉट **उस चैनल में Admin है।**",
                    reply_to_message_id=status_msg.message_id
                 )


    final_text = f"✅ काम पूरा हुआ! ({target_chat_id})\nसफल: {success_count}\nअसफल: {error_count}"
    await status_msg.edit_text(final_text)
    
    await delete_message_after_delay(status_msg)

    return ConversationHandler.END


# --- ADMIN MANAGEMENT FLOWS (CHANNEL AND USER) ---

async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एडमिन पैनल के सभी कॉलबैक (Add/Remove Admin/Channel) को हैंडल करता है"""
    query = update.callback_query
    await query.answer()
    
    # पुराने मैसेज को हटाने की कोशिश करें ताकि 'Bad Request' error न आए
    try:
        await query.message.delete()
    except Exception:
        pass
        
    data = query.data
    
    # --- CHANNEL MANAGEMENT ---
    if data == 'admin_add_channel':
        # नया मैसेज भेजें (एडिट नहीं)
        await query.message.reply_text(
            '➕ चैनल जोड़ने के लिए, मुझे इस फॉर्मेट में मैसेज भेजें:\n\n'
            '`चैनल का नाम,चैनल ID`\n\n'
            '**उदाहरण:** `My Quiz Channel,-100123456789`\n\n'
            'या `/cancel` दबाकर बाहर आएं।'
        , parse_mode='Markdown')
        return AWAITING_CHANNEL_INFO
    
    elif data == 'admin_delete_channel':
        channels = get_channels()
        if not channels:
            await query.message.reply_text("❌ कोई चैनल जोड़ा नहीं गया है।")
            return ConversationHandler.END

        keyboard = [[InlineKeyboardButton(name, callback_data=f'delete_channel_{chat_id}')] for chat_id, name in channels]
        keyboard.append([InlineKeyboardButton("🔙 वापस जाएं", callback_data='admin_panel')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text('🗑️ डिलीट करने के लिए चैनल चुनें:', reply_markup=reply_markup)
        return SELECTING_CHANNEL 

    elif data.startswith('delete_channel_'):
        chat_id_to_delete = data.replace('delete_channel_', '')
        if manage_channel(chat_id_to_delete, action='remove'):
            msg = await query.message.reply_text(f"✅ चैनल ID `{chat_id_to_delete}` हटा दिया गया है।")
        else:
            msg = await query.message.reply_text("❌ चैनल हटाने में त्रुटि हुई।")
        await delete_message_after_delay(msg)
        return ConversationHandler.END

    # --- ADMIN MANAGEMENT ---
    elif data == 'admin_manage_admins':
        admins = get_all_admins()
        admin_list = "\n".join([f"- `{uid}`" for uid in admins])
        keyboard = [
            [InlineKeyboardButton("➕ नया एडमिन जोड़ें", callback_data='admin_add_user')],
            [InlineKeyboardButton("➖ एडमिन हटाएँ", callback_data='admin_remove_user')],
            [InlineKeyboardButton("🔙 वापस जाएं", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            f'👥 वर्तमान एडमिन:\n{admin_list}\n\nक्या करना है चुनें:', 
            reply_markup=reply_markup, 
            parse_mode='Markdown'
        )
        return SELECTING_CHANNEL 

    elif data in ['admin_add_user', 'admin_remove_user']:
        context.user_data['admin_action'] = data.split('_')[1] # 'add' or 'remove'
        action_name = "जोड़ने" if context.user_data['admin_action'] == 'add' else "हटाने"
        await query.message.reply_text(
            f'कृपया उस यूज़र की **Telegram User ID** भेजें जिसे आप एडमिन {action_name} चाहते हैं।\n'
            'उदाहरण: `987654321`'
        )
        return AWAITING_ADMIN_ID

    elif data == 'admin_panel':
        # यह बटन admin_panel फ़ंक्शन को दोबारा कॉल करेगा (नया मैसेज भेजने के लिए)
        return await admin_panel(update, context)

    return ConversationHandler.END


async def handle_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """चैनल जोड़ने का इनपुट हैंडल करता है"""
    try:
        name, chat_id = [p.strip() for p in update.message.text.split(',')]
        if not chat_id.startswith('-100'):
            raise ValueError("चैनल ID -100 से शुरू होना चाहिए।")
        
        if manage_channel(chat_id, name, action='add'):
            msg = await update.message.reply_text(f"✅ चैनल **{name}** (`{chat_id}`) सफलतापूर्वक जोड़ दिया गया है।", parse_mode='Markdown')
        else:
            msg = await update.message.reply_text("❌ त्रुटि: चैनल ID पहले से मौजूद है।")
        
    except ValueError as e:
        msg = await update.message.reply_text(f"❌ त्रुटि: इनपुट फॉर्मेट गलत है। सही फॉर्मेट है: `चैनल का नाम,-100123456789`\n\nReason: {e}", parse_mode='Markdown')
    except Exception:
        msg = await update.message.reply_text("❌ सामान्य त्रुटि।")

    await delete_message_after_delay(msg)
    return ConversationHandler.END

async def handle_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एडमिन जोड़ने/हटाने का इनपुट हैंडल करता है"""
    try:
        target_id = int(update.message.text.strip())
        action = context.user_data.get('admin_action')
        
        if target_id == SUPER_ADMIN_ID and action == 'remove':
            msg = await update.message.reply_text("⚠️ आप Super Admin को हटा नहीं सकते हैं।")
        elif manage_admin(target_id, action):
            action_name = "जोड़ दिया गया" if action == 'add' else "हटा दिया गया"
            msg = await update.message.reply_text(f"✅ यूज़र ID `{target_id}` सफलतापूर्वक एडमिन से {action_name} है।", parse_mode='Markdown')
        else:
            msg = await update.message.reply_text("❌ त्रुटि: कोई बदलाव नहीं हुआ (शायद ID पहले से ही उस लिस्ट में है)।")
            
    except ValueError:
        msg = await update.message.reply_text("❌ त्रुटि: कृपया सिर्फ संख्या (User ID) डालें।")

    await delete_message_after_delay(msg)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Conversation को कैंसिल करता है"""
    await update.message.reply_text('❌ ऑपरेशन कैंसिल किया गया।')
    return ConversationHandler.END


# --- MAIN EXECUTION ---

if __name__ == '__main__':
    setup_db()
    
    # GUARANTEED एडमिन रजिस्ट्रेशन
    if SUPER_ADMIN_ID and SUPER_ADMIN_ID == 1347057415:
        manage_admin(SUPER_ADMIN_ID, 'add')
        print(f"✅ {SUPER_ADMIN_ID} को एडमिन के रूप में सुनिश्चित किया गया।")
    else:
        print(f"❌ WARNING: SUPER_ADMIN_ID (1347057415) लोड नहीं हुआ। /admin काम नहीं करेगा।")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    admin_control_conversation = ConversationHandler(
        entry_points=[
            CommandHandler('quiz', start_quiz_flow),
            CommandHandler('admin', admin_panel),
            # सभी बटन क्लिक्स को handle_admin_callbacks पर भेजें
            CallbackQueryHandler(handle_admin_callbacks, pattern='^admin_add_channel$|^admin_delete_channel$|^admin_manage_admins$|^admin_add_user$|^admin_remove_user$|^delete_channel_') 
        ],
        states={
            SELECTING_CHANNEL: [
                CallbackQueryHandler(select_channel, pattern='^select_channel_'),
                CallbackQueryHandler(handle_admin_callbacks), # Fallback for delete channel selection
            ],
            AWAITING_QUIZ_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_quiz_text)
            ],
            AWAITING_CHANNEL_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_info)
            ],
            AWAITING_ADMIN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_id)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation), CallbackQueryHandler(admin_panel, pattern='^admin_panel$')],
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(admin_control_conversation)

    print("🤖 बॉट स्टार्ट हो गया है...")
    application.run_polling()