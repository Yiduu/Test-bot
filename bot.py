import os 
import logging
import psycopg2
from urllib.parse import quote
from psycopg2 import sql, IntegrityError, ProgrammingError
from psycopg2.extras import RealDictCursor
from pathlib import Path
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, 
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest
import threading
from flask import Flask, jsonify 
from contextlib import closing
from datetime import datetime
import random
import time
from typing import Optional

# Load environment variables first
load_dotenv()

# Initialize database connection
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv('TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_ID = os.getenv('ADMIN_ID')

# Initialize database tables with schema migration
def init_db():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as c:
                
                # ---------------- Create Tables ----------------
                c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    anonymous_name TEXT,
                    sex TEXT DEFAULT '👤',
                    awaiting_name BOOLEAN DEFAULT FALSE,
                    waiting_for_post BOOLEAN DEFAULT FALSE,
                    waiting_for_comment BOOLEAN DEFAULT FALSE,
                    selected_category TEXT,
                    comment_post_id INTEGER,
                    comment_idx INTEGER,
                    reply_idx INTEGER,
                    nested_idx INTEGER,
                    notifications_enabled BOOLEAN DEFAULT TRUE,
                    privacy_public BOOLEAN DEFAULT TRUE,
                    is_admin BOOLEAN DEFAULT FALSE,
                    waiting_for_private_message BOOLEAN DEFAULT FALSE,
                    private_message_target TEXT,
                    aura_points INTEGER DEFAULT 0
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS followers (
                    follower_id TEXT,
                    followed_id TEXT,
                    PRIMARY KEY (follower_id, followed_id)
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    post_id SERIAL PRIMARY KEY,
                    content TEXT,
                    author_id TEXT,
                    category TEXT,
                    channel_message_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    media_type TEXT DEFAULT 'text',
                    media_id TEXT,
                    comment_count INTEGER DEFAULT 0,
                    approved BOOLEAN DEFAULT FALSE,
                    admin_approved_by TEXT,
                    thread_from_post_id BIGINT DEFAULT NULL
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS comments (
                    comment_id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES posts(post_id),
                    parent_comment_id INTEGER DEFAULT 0,
                    author_id TEXT,
                    content TEXT,
                    type TEXT DEFAULT 'text',
                    file_id TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS reactions (
                    reaction_id SERIAL PRIMARY KEY,
                    comment_id INTEGER REFERENCES comments(comment_id),
                    user_id TEXT,
                    type TEXT,
                    UNIQUE(comment_id, user_id)
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS private_messages (
                    message_id SERIAL PRIMARY KEY,
                    sender_id TEXT REFERENCES users(user_id),
                    receiver_id TEXT REFERENCES users(user_id),
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT FALSE
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS blocks (
                    blocker_id TEXT REFERENCES users(user_id),
                    blocked_id TEXT REFERENCES users(user_id),
                    PRIMARY KEY (blocker_id, blocked_id)
                )
                ''')

                # ---------------- Create admin user if specified ----------------
                if ADMIN_ID:
                    c.execute('''
                        INSERT INTO users (user_id, anonymous_name, is_admin)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE
                    ''', (ADMIN_ID, "Admin"))

            conn.commit()
        logging.info("PostgreSQL database initialized successfully")
    except Exception as e:
        logging.error(f"Database initialization failed: {e}")

# Database helper functions - FIXED VERSION
# -------------------- PostgreSQL Connection Pool --------------------
from psycopg2 import pool

# Create a global connection pool (reuses DB connections instead of reconnecting every time)
try:
    db_pool = pool.SimpleConnectionPool(
        1, 10,  # min 1, max 10 connections
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    logging.info("✅ Database connection pool created successfully")
except Exception as e:
    logging.error(f"❌ Failed to create database pool: {e}")
    db_pool = None


def db_execute(query, params=(), fetch=False, fetchone=False):
    """Execute a SQL query using the global connection pool."""
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch:
                result = cur.fetchall()
            elif fetchone:
                result = cur.fetchone()
            else:
                result = True
            conn.commit()
            return result
    except Exception as e:
        logging.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            db_pool.putconn(conn)


def db_fetch_one(query, params=()):
    return db_execute(query, params, fetchone=True)

def db_fetch_all(query, params=()):
    return db_execute(query, params, fetch=True)

# -------------------- Aura Points: calculation and helpers --------------------
# Formula: aura_points = (approved_posts * 5) + (comments * 2) + (received_likes * 1)

def calculate_aura_points(user_id: str) -> int:
    """Compute aura points from DB metrics (always reads current state)."""
    try:
        posts_row = db_fetch_one(
            "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
            (user_id,)
        )
        posts = posts_row['count'] if posts_row else 0

        comments_row = db_fetch_one(
            "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
            (user_id,)
        )
        comments = comments_row['count'] if comments_row else 0

        # received_likes = number of 'like' reactions on comments authored by user
        likes_row = db_fetch_one('''
            SELECT COUNT(r.reaction_id) as cnt
            FROM reactions r
            JOIN comments c ON r.comment_id = c.comment_id
            WHERE c.author_id = %s AND r.type = 'like'
        ''', (user_id,))
        received_likes = likes_row['cnt'] if likes_row else 0

        aura = (posts * 5) + (comments * 2) + (received_likes * 1)
        return int(aura)
    except Exception as e:
        logging.error(f"Error calculating aura for {user_id}: {e}")
        return 0

def update_user_aura(user_id: str):
    """Recalculate and persist aura_points for a user."""
    try:
        aura = calculate_aura_points(user_id)
        db_execute("UPDATE users SET aura_points = %s WHERE user_id = %s", (aura, user_id))
        return aura
    except Exception as e:
        logging.error(f"Failed to update aura for {user_id}: {e}")
        return None

def get_user_aura(user_id: str) -> int:
    """Return stored aura_points; fallback to calculated value if missing."""
    try:
        row = db_fetch_one("SELECT aura_points FROM users WHERE user_id = %s", (user_id,))
        if row and row.get('aura_points') is not None:
            return int(row['aura_points'])
        # fallback
        aura = calculate_aura_points(user_id)
        db_execute("UPDATE users SET aura_points = %s WHERE user_id = %s", (aura, user_id))
        return aura
    except Exception as e:
        logging.error(f"Error fetching aura for {user_id}: {e}")
        return 0

# Categories
CATEGORIES = [
    ("🙏 Pray For Me", "PrayForMe"),
    ("📖 Bible", "Bible"),
    ("💼 Work and Life", "WorkLife"),
    ("🕊 Spiritual Life", "SpiritualLife"),
    ("⚔️ Christian Challenges", "ChristianChallenges"),
    ("❤️ Relationship", "Relationship"),
    ("💍 Marriage", "Marriage"),
    ("🧑‍🤝‍🧑 Youth", "Youth"),
    ("💰 Finance", "Finance"),
    ("🔖 Other", "Other"),
] 

def build_category_buttons():
    buttons = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for j in range(2):
            if i + j < len(CATEGORIES):
                name, code = CATEGORIES[i + j]
                row.append(InlineKeyboardButton(name, callback_data=f'category_{code}'))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) 

# Initialize Flask app for Render health checks
flask_app = Flask(__name__) 

@flask_app.route('/')
def health_check():
    return jsonify(status="OK", message="Christian Chat Bot is running") 

@flask_app.route('/ping')
def uptimerobot_ping():
    return jsonify(status="OK", message="Pong! Bot is alive") 

# Create main menu keyboard with improved buttons
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🌟 Share My Thoughts")],
        [KeyboardButton("👤 View Profile"), KeyboardButton("📚 My Previous Posts")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("❓ Help")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
) 

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__) 

def create_anonymous_name(user_id):
    try:
        uid_int = int(user_id)
    except ValueError:
        uid_int = abs(hash(user_id)) % 10000
    names = ["Anonymous", "Believer", "Christian", "Servant", "Disciple", "ChildOfGod"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def calculate_user_rating(user_id):
    # Keep rating for backwards compatibility (simple contributions)
    post_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
        (user_id,)
    )
    post_count = post_row['count'] if post_row else 0
    
    comment_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
        (user_id,)
    )
    comment_count = comment_row['count'] if comment_row else 0
    
    return post_count + comment_count

def count_all_comments(post_id):
    def count_replies(parent_id=None):
        if parent_id is None:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE post_id = %s AND parent_comment_id = 0",
                (post_id,)
            )
        else:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE parent_comment_id = %s",
                (parent_id,)
            )
        
        total = len(comments)
        for comment in comments:
            total += count_replies(comment['comment_id'])
        return total
    
    return count_replies()

def get_display_name(user_data):
    if user_data and user_data.get('anonymous_name'):
        return user_data['anonymous_name']
    return "Anonymous"

def get_display_sex(user_data):
    if user_data and user_data.get('sex'):
        return user_data['sex']
    return '👤'

def get_user_rank(user_id):
    users = db_fetch_all('''
        SELECT user_id, 
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
    ''')
    
    for rank, user in enumerate(users, start=1):
        if user['user_id'] == user_id:
            return rank
    return None

# UI helpers
def md_escape(text: str) -> str:
    return escape_markdown(text or "", version=2)

def profile_card_text(display_name: str, display_sex: str, rating: int, aura: int, followers_count: int) -> str:
    # Clean, modern profile card
    return (
        f"👤 *{md_escape(display_name)}* {display_sex}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Contributions:* {rating}\n"
        f"✨ *Aura Points:* {aura}\n"
        f"👥 *Followers:* {followers_count}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_Use /menu to return_"
    )

def post_card_text(category: str, snippet: str, comment_count: int) -> str:
    return (
        f"📝 *{md_escape(category)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"❝ {md_escape(snippet)} ❞\n\n"
        f"💬 Comments: {comment_count}\n"
        f"──────────────────"
    )

# Initialize logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ---------- Custom error handler ----------
async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

# ---------- Set up bot commands ----------
async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("menu", "📱 Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Share your thoughts"),
        BotCommand("leaderboard", "View top contributors"),
        BotCommand("settings", "Configure your preferences"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
        BotCommand("inbox", "View your private messages"),
    ]
    
    if ADMIN_ID:
        commands.append(BotCommand("admin", "Admin panel (admin only)"))
    
    await app.bot.set_my_commands(commands)

# ---------- Main entry point ----------
def main():
    # Initialize database before starting the bot
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("inbox", show_inbox))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    # Start polling
    app.run_polling() 

if __name__ == "__main__": 
    # Initialize database first
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        exit(1)
    
    # Start Flask server in a separate thread for Render
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    # Start Telegram bot in main thread
    main()
