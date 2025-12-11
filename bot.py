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
                    private_message_target TEXT
                )
                ''')

                # Ensure aura_points exists for compatibility/migrations
                c.execute("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS aura_points INTEGER DEFAULT 0
                """)

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

# -------------------- Aura Points helpers --------------------
# Formula: aura_points = (approved_posts * 5) + (comments * 2) + (received_likes * 1)
def calculate_aura_points(user_id: str) -> int:
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

def update_user_aura(user_id: str) -> int:
    try:
        aura = calculate_aura_points(user_id)
        db_execute("UPDATE users SET aura_points = %s WHERE user_id = %s", (aura, user_id))
        return aura
    except Exception as e:
        logging.error(f"Failed to update aura for {user_id}: {e}")
        return 0

def get_user_aura(user_id: str) -> int:
    try:
        row = db_fetch_one("SELECT aura_points FROM users WHERE user_id = %s", (user_id,))
        if row and row.get('aura_points') is not None:
            return int(row['aura_points'])
        return update_user_aura(user_id)
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

def format_stars(rating, max_stars=5):
    full_stars = min(rating // 5, max_stars)
    empty_stars = max(0, max_stars - full_stars)
    return '⭐️' * full_stars + '☆' * empty_stars

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

async def update_channel_post_comment_count(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Update the comment count on the channel post"""
    try:
        # Get the post details
        post = db_fetch_one("SELECT channel_message_id, comment_count FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post['channel_message_id']:
            return
        
        # Count all comments for this post
        total_comments = count_all_comments(post_id)
        
        # Update the database with the new count
        db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (total_comments, post_id))
        
        # Update the channel message button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Try to edit the message in the channel
        await context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=post['channel_message_id'],
            reply_markup=keyboard
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Failed to update comment count in channel: {e}")
    except Exception as e:
        logger.error(f"Error updating channel post comment count: {e}")

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
        LIMIT 10
    ''')
    
    leaderboard_text = "🏆 *Top Contributors* 🏆\n\n"
    for idx, user in enumerate(top_users, start=1):
        stars = format_stars(user['total'] // 5)
        # fetch aura for display
        aura = get_user_aura(user['user_id']) if user and user.get('user_id') else 0
        leaderboard_text += (
            f"{idx}. {user['anonymous_name']} {user['sex']} - {user['total']} contributions • ✨ Aura: {aura} {stars}\n"
        )
    
    user_id = str(update.effective_user.id)
    user_rank = get_user_rank(user_id)
    
    if user_rank and user_rank > 10:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = calculate_user_rating(user_id)
            leaderboard_text += (
                f"\n...\n"
                f"{user_rank}. {user_data['anonymous_name']} {user_data['sex']} - {user_contributions} contributions\n"
            )
    
    keyboard = [
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')]
    ]
    
    if update.message:
        await update.message.reply_text(
            leaderboard_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                leaderboard_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await update.callback_query.message.reply_text(
                leaderboard_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )

# (rest of the file remains functionally the same with small aura update calls integrated below)

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    try:
        user = db_fetch_one("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = %s", (user_id,))
        
        if not user:
            if update.message:
                await update.message.reply_text("Please use /start first to initialize your profile.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("Please use /start first to initialize your profile.")
            return
        
        notifications_status = "✅ ON" if user['notifications_enabled'] else "❌ OFF"
        privacy_status = "🌍 Public" if user['privacy_public'] else "🔒 Private"
        
        keyboard = [
            [
                InlineKeyboardButton(f"🔔 Notifications: {notifications_status}", 
                                   callback_data='toggle_notifications')
            ],
            [
                InlineKeyboardButton(f"👁‍🗨 Privacy: {privacy_status}", 
                                   callback_data='toggle_privacy')
            ],
            [
                InlineKeyboardButton("📱 Main Menu", callback_data='menu'),
                InlineKeyboardButton("👤 Profile", callback_data='profile')
            ]
        ]
        
        # Add admin panel button if user is admin
        if user['is_admin']:
            keyboard.insert(0, [InlineKeyboardButton("🛠 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    "⚙️ *Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.callback_query.message.reply_text(
                    "⚙️ *Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text(
                "⚙️ *Settings Menu*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Error in show_settings: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading settings. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error loading settings. Please try again.")

# send_post_confirmation and notify_* functions are unchanged (kept above) - omitted here for brevity in this snippet

# ---------- Approve post: recalc aura after approval ----------
async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        try:
            await query.answer("❌ You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("❌ You don't have permission to do this.")
        return
    
    # Get the post
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await query.answer("❌ Post not found.", show_alert=True)
        except:
            await query.edit_message_text("❌ Post not found.")
        return
    
    try:
        # Format the post content for the channel
        hashtag = f"#{post['category']}"
        caption_text = (
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
        # Create the comments button
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Check if this is a thread continuation
        reply_to_message_id = None
        if post['thread_from_post_id']:
            # Get the original post's channel message ID
            original_post = db_fetch_one(
                "SELECT channel_message_id FROM posts WHERE post_id = %s", 
                (post['thread_from_post_id'],)
            )
            if original_post and original_post['channel_message_id']:
                reply_to_message_id = original_post['channel_message_id']
        
        # Send post to channel based on media type
        if post['media_type'] == 'text':
            msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        elif post['media_type'] == 'photo':
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        elif post['media_type'] == 'voice':
            msg = await context.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        else:
            await query.answer("❌ Unsupported media type.", show_alert=True)
            return
        
        # Update the post in database
        success = db_execute(
            "UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s WHERE post_id = %s",
            (user_id, msg.message_id, post_id)
        )
        
        if not success:
            await query.answer("❌ Failed to update database.", show_alert=True)
            return

        # Recalculate aura for the author (approved_posts changed)
        try:
            update_user_aura(post['author_id'])
        except Exception as e:
            logger.error(f"Error updating aura after approval: {e}")
        
        # Notify the author
        try:
            await context.bot.send_message(
                chat_id=post['author_id'],
                text="✅ Your post has been approved and published!"
            )
        except Exception as e:
            logger.error(f"Error notifying author: {e}")
        
        # Update the admin's message
        try:
            await query.edit_message_text(
                f"✅ Post approved and published!\n\n{post['content'][:100]}...",
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await query.message.reply_text(
                f"✅ Post approved and published!\n\n{post['content'][:100]}...",
                parse_mode=ParseMode.MARKDOWN
            )
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        try:
            await query.answer(f"❌ Failed to approve post: {str(e)}", show_alert=True)
        except:
            await query.edit_message_text("❌ Failed to approve post. Please try again.")

# ---------- Button handler: reactions update aura ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Error answering callback query: {e}")
    
    user_id = str(query.from_user.id)

    try:
        # many branches above omitted for brevity — only reaction branch shown with aura integration
        if query.data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
            try:
                parts = query.data.split('_')
                comment_id = int(parts[1])
                reaction_type = 'like' if parts[0] in ('likecomment', 'likereply') else 'dislike'

                # Check if user already has a reaction on this comment
                existing_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                )

                if existing_reaction:
                    if existing_reaction['type'] == reaction_type:
                        # User is clicking the same reaction - remove it (toggle off)
                        db_execute(
                            "DELETE FROM reactions WHERE comment_id = %s AND user_id = %s",
                            (comment_id, user_id)
                        )
                    else:
                        # User is changing reaction - update it
                        db_execute(
                            "UPDATE reactions SET type = %s WHERE comment_id = %s AND user_id = %s",
                            (reaction_type, comment_id, user_id)
                        )
                else:
                    # User is adding a new reaction
                    db_execute(
                        "INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)",
                        (comment_id, user_id, reaction_type)
                    )

                # Fetch comment to get author
                comment = db_fetch_one(
                    "SELECT post_id, parent_comment_id, author_id, type, content FROM comments WHERE comment_id = %s",
                    (comment_id,)
                )
                if not comment:
                    await query.answer("Comment not found", show_alert=True)
                    return

                # Recalculate aura for the comment author (received_likes may have changed)
                try:
                    update_user_aura(comment['author_id'])
                except Exception as e:
                    logger.error(f"Error updating aura after reaction change: {e}")

                # Get updated counts
                likes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'",
                    (comment_id,)
                )
                likes = likes_row['cnt'] if likes_row else 0
                
                dislikes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'",
                    (comment_id,)
                )
                dislikes = dislikes_row['cnt'] if dislikes_row else 0

                post_id = comment['post_id']
                parent_comment_id = comment['parent_comment_id']

                # Get user's current reaction after update
                user_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                )

                like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
                dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

                if parent_comment_id == 0:
                    # Build keyboard with edit/delete buttons for author
                    kb_buttons = [
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment_id}")
                        ]
                    ]
                    
                    # Add edit/delete buttons only for comment author and only for text comments
                    if comment['author_id'] == user_id:
                        if comment['type'] == 'text':
                            kb_buttons.append([
                                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                    
                    new_kb = InlineKeyboardMarkup(kb_buttons)
                else:
                    # Build keyboard for replies with edit/delete buttons for author
                    kb_buttons = [
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likereply_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikereply_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{comment_id}")
                        ]
                    ]
                    
                    # Add edit/delete buttons only for reply author and only for text comments
                    if comment['author_id'] == user_id:
                        if comment['type'] == 'text':
                            kb_buttons.append([
                                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                    
                    new_kb = InlineKeyboardMarkup(kb_buttons)

                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        reply_markup=new_kb
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        logger.error(f"Error updating reaction buttons: {e}")
                
                # Send notification only if reaction was added (not removed)
                if not existing_reaction or existing_reaction['type'] != reaction_type:
                    comment_author = db_fetch_one(
                        "SELECT user_id, notifications_enabled FROM users WHERE user_id = %s",
                        (comment['author_id'],)
                    )
                    if comment_author and comment_author['notifications_enabled'] and comment_author['user_id'] != user_id:
                        reactor_name = get_display_name(
                            db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                        )
                        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
                        
                        notification_text = (
                            f"❤️ {reactor_name} reacted to your comment:\n\n"
                            f"🗨 {escape_markdown(comment['content'][:100], version=2)}\n\n"
                            f"📝 Post: {escape_markdown(post_preview, version=2)}\n\n"
                            f"[View conversation](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
                        )
                        
                        await context.bot.send_message(
                            chat_id=comment_author['user_id'],
                            text=notification_text,
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
            except Exception as e:
                logger.error(f"Error processing reaction: {e}")
                await query.answer("❌ Error updating reaction", show_alert=True)
            return

    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        try:
            await query.message.reply_text("❌ An error occurred. Please try again.")
        except:
            pass

# ---------- handle_message: recalc aura after commenting and posting ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # NEW: Handle comment editing
    if 'editing_comment' in context.user_data:
        comment_id = context.user_data['editing_comment']
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        
        if comment and comment['author_id'] == user_id and comment['type'] == 'text':
            # Update the comment
            db_execute(
                "UPDATE comments SET content = %s WHERE comment_id = %s",
                (text, comment_id)
            )
            
            # Clean up
            del context.user_data['editing_comment']
            
            await update.message.reply_text(
                "✅ Comment updated successfully!",
                reply_markup=main_menu
            )
            return
        else:
            del context.user_data['editing_comment']
            await update.message.reply_text(
                "❌ Error updating comment. Please try again.",
                reply_markup=main_menu
            )
            return

    # If user doesn't exist, create them
    if not user:
        anon = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        db_execute(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        )
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # NEW: Check if we have a thread_from_post_id for continuation
    thread_from_post_id = context.user_data.get('thread_from_post_id')
    
    if user and user['waiting_for_post']:
        category = user['selected_category']
        db_execute(
            "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
            (user_id,)
        )
        
        post_content = ""
        media_type = 'text'
        media_id = None
        
        try:
            if update.message.text:
                post_content = update.message.text
                await send_post_confirmation(update, context, post_content, category, thread_from_post_id=thread_from_post_id)
                return
            elif update.message.photo:
                photo = update.message.photo[-1]
                media_id = photo.file_id
                media_type = 'photo'
                post_content = update.message.caption or ""
            elif update.message.voice:
                voice = update.message.voice
                media_id = voice.file_id
                media_type = 'voice'
                post_content = update.message.caption or ""
            else:
                post_content = "(Unsupported content type)"
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            post_content = "(Unsupported content type)" 

        await send_post_confirmation(update, context, post_content, category, media_type, media_id, thread_from_post_id=thread_from_post_id)
        return

    elif user and user['waiting_for_comment']:
        post_id = user['comment_post_id']
    
        parent_comment_id = 0
        if user['comment_idx']:
            try:
                parent_comment_id = int(user['comment_idx'])
            except Exception:
                parent_comment_id = 0
    
        comment_type = 'text'
        file_id = None
        content = ""
    
        if update.message.text:
            content = update.message.text
            comment_type = 'text'
        elif update.message.voice:
            voice = update.message.voice
            file_id = voice.file_id
            comment_type = 'voice'
            content = update.message.caption or ""
        elif update.message.animation:  # GIF
            animation = update.message.animation
            file_id = animation.file_id
            comment_type = 'gif'
            content = update.message.caption or ""
        elif update.message.sticker:
            sticker = update.message.sticker
            file_id = sticker.file_id
            comment_type = 'sticker'
            content = ""  # Stickers don't have text content
        elif update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            comment_type = 'photo'
            content = update.message.caption or ""
        else:
            await update.message.reply_text("❌ Unsupported comment type. Please send text, voice, GIF, sticker, or photo.")
            return
    
        # Insert new comment
        comment_row = db_execute(
            """INSERT INTO comments 
            (post_id, parent_comment_id, author_id, content, type, file_id) 
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING comment_id""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id),
            fetchone=True
        )

        # Recalculate aura for commenter (comments contribute to aura)
        try:
            update_user_aura(user_id)
        except Exception as e:
            logger.error(f"Error updating aura after comment: {e}")
    
        # Reset state
        db_execute(
            "UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL WHERE user_id = %s",
            (user_id,)
        )
    
        await update.message.reply_text("✅ Your comment has been posted!", reply_markup=main_menu)
        
        # Update comment count
        await update_channel_post_comment_count(context, post_id)
        
        # Notify parent comment author if this is a reply
        if parent_comment_id != 0:
            await notify_user_of_reply(context, post_id, parent_comment_id, user_id)
        return

    # (rest of handle_message unchanged)
    if user and user['awaiting_name']:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s",
                (new_name, user_id)
            )
            await update.message.reply_text(f"✅ Name updated to *{new_name}*!", parse_mode=ParseMode.MARKDOWN)
            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle main menu buttons
    if text == "🌟 Share My Thoughts":
        await update.message.reply_text(
            "📚 *Choose a category:*",
            reply_markup=build_category_buttons(),
            parse_mode=ParseMode.MARKDOWN
        )
        return 

    elif text == "👤 View Profile":
        await send_updated_profile(user_id, update.message.chat.id, context)
        return 

    elif text == "🏆 Leaderboard":
        await show_leaderboard(update, context)
        return

    elif text == "⚙️ Settings":
        await show_settings(update, context)
        return

    elif text == "📚 My Previous Posts":
        await show_my_content_menu(update, context)  # Show menu instead of direct posts
        return

    elif text == "❓ Help":
        help_text = (
            "ℹ️ *How to Use This Bot:*\n"
            "• Use the menu buttons to navigate.\n"
            "• Tap 'Share My Thoughts' to share your thoughts anonymously.\n"
            "• Choose a category and type or send your message (text, photo, or voice).\n"
            "• After posting, others can comment on your posts.\n"
            "• View your profile, set your name and sex anytime.\n"
            "• Use 'My Previous Posts' to view and continue your past posts.\n"
            "• Use the comments button on channel posts to join the conversation here.\n"
            "• Follow users to send them private messages."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
        return 

    # If none of the above, show main menu
    await update.message.reply_text(
        "How can I help you?",
        reply_markup=main_menu
    )

# Note: confirm_post branch in button_handler (where posts are inserted) should recalc aura for the author.
# That branch exists earlier in button_handler; ensure update_user_aura(user_id) is called after inserting posts.
# For brevity, the remaining unchanged handlers and main() function follow the original file.

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

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
