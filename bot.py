import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
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
from datetime import datetime
import time

# Load environment variables
load_dotenv()

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv('TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_ID = os.getenv('ADMIN_ID')

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database connection pool
db_pool = None

def init_db_pool():
    """Initialize database connection pool."""
    global db_pool
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 10,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor
        )
        logger.info("✅ Database connection pool created")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to create database pool: {e}")
        return False

def get_db_connection():
    """Get a database connection from the pool."""
    if db_pool:
        return db_pool.getconn()
    return None

def return_db_connection(conn):
    """Return a database connection to the pool."""
    if db_pool and conn:
        db_pool.putconn(conn)

def db_execute(query, params=(), fetch=False, fetchone=False):
    """Execute a SQL query safely."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None
            
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
        logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        return_db_connection(conn)

def db_fetch_one(query, params=()):
    return db_execute(query, params, fetchone=True)

def db_fetch_all(query, params=()):
    return db_execute(query, params, fetch=True)

def init_database():
    """Initialize database tables."""
    try:
        # Users table
        db_execute('''
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
                notifications_enabled BOOLEAN DEFAULT TRUE,
                privacy_public BOOLEAN DEFAULT TRUE,
                is_admin BOOLEAN DEFAULT FALSE,
                waiting_for_private_message BOOLEAN DEFAULT FALSE,
                private_message_target TEXT
            )
        ''')
        
        # Posts table with thread support
        db_execute('''
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
        
        # Comments table
        db_execute('''
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
        
        # Reactions table
        db_execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                reaction_id SERIAL PRIMARY KEY,
                comment_id INTEGER REFERENCES comments(comment_id),
                user_id TEXT,
                type TEXT,
                UNIQUE(comment_id, user_id)
            )
        ''')
        
        # Followers table
        db_execute('''
            CREATE TABLE IF NOT EXISTS followers (
                follower_id TEXT,
                followed_id TEXT,
                PRIMARY KEY (follower_id, followed_id)
            )
        ''')
        
        # Private messages table
        db_execute('''
            CREATE TABLE IF NOT EXISTS private_messages (
                message_id SERIAL PRIMARY KEY,
                sender_id TEXT,
                receiver_id TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Blocks table
        db_execute('''
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id TEXT,
                blocked_id TEXT,
                PRIMARY KEY (blocker_id, blocked_id)
            )
        ''')
        
        logger.info("✅ Database tables initialized successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return False

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
    """Build category selection keyboard."""
    buttons = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for j in range(2):
            if i + j < len(CATEGORIES):
                name, code = CATEGORIES[i + j]
                row.append(InlineKeyboardButton(name, callback_data=f'category_{code}'))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

# Flask app for health checks
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return jsonify(status="OK", message="Christian Chat Bot is running")

@flask_app.route('/ping')
def ping():
    return jsonify(status="OK", message="Pong! Bot is alive")

# Main menu with improved buttons
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

def create_anonymous_name(user_id):
    """Create anonymous name for user."""
    try:
        uid_int = int(user_id)
    except ValueError:
        uid_int = abs(hash(user_id)) % 10000
    names = ["Anonymous", "Believer", "Christian", "Servant", "Disciple", "ChildOfGod"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def get_display_name(user_data):
    """Get display name from user data."""
    if user_data and user_data.get('anonymous_name'):
        return user_data['anonymous_name']
    return "Anonymous"

def get_display_sex(user_data):
    """Get display sex from user data."""
    if user_data and user_data.get('sex'):
        return user_data['sex']
    return '👤'

def calculate_user_rating(user_id):
    """Calculate user rating based on posts and comments."""
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
    """Format rating as stars."""
    full_stars = min(rating // 5, max_stars)
    empty_stars = max(0, max_stars - full_stars)
    return '⭐️' * full_stars + '☆' * empty_stars

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = str(update.effective_user.id)
    
    # Initialize user if not exists
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        anon_name = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        success = db_execute(
            "INSERT INTO users (user_id, anonymous_name, is_admin) VALUES (%s, %s, %s)",
            (user_id, anon_name, is_admin)
        )
        if not success:
            await update.message.reply_text("❌ Error creating user profile.")
            return
    
    # Handle deep linking
    args = context.args
    if args:
        arg = args[0]
        
        if arg.startswith("comments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                await show_comments_menu(update, context, post_id)
            return
            
        elif arg.startswith("profileid_"):
            target_user_id = arg.split("_", 1)[1]
            await show_user_profile(update, context, target_user_id)
            return
    
    # Show main menu
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu."""
    keyboard = [
        [
            InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile", callback_data='profile')
        ],
        [
            InlineKeyboardButton("📚 My Previous Posts", callback_data='previous_posts'),
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard')
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
            InlineKeyboardButton("❓ Help", callback_data='help')
        ]
    ]
    
    if update.message:
        await update.message.reply_text(
            "🌟✝️ *Welcome to Christian Vent* ✝️🌟\n\n"
            "Share your thoughts anonymously with the Christian community.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "You can use the buttons below:",
            reply_markup=main_menu
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "🌟✝️ *Welcome to Christian Vent* ✝️🌟\n\n"
            "Share your thoughts anonymously with the Christian community.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def show_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: str):
    """Show user profile."""
    user_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (target_user_id,))
    if not user_data:
        await update.message.reply_text("❌ User not found.")
        return
    
    current_user_id = str(update.effective_user.id)
    display_name = get_display_name(user_data)
    display_sex = get_display_sex(user_data)
    rating = calculate_user_rating(target_user_id)
    stars = format_stars(rating)
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = %s",
        (target_user_id,)
    )
    
    # Build buttons
    buttons = []
    if target_user_id != current_user_id:
        is_following = db_fetch_one(
            "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
            (current_user_id, target_user_id)
        )
        if is_following:
            buttons.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{target_user_id}')])
            buttons.append([InlineKeyboardButton("✉️ Send Message", callback_data=f'message_{target_user_id}')])
        else:
            buttons.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{target_user_id}')])
    
    buttons.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    profile_text = (
        f"👤 *{display_name}* 🎖\n"
        f"📌 Sex: {display_sex}\n"
        f"⭐️ Rating: {rating} {stars}\n"
        f"👥 Followers: {len(followers)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    await update.message.reply_text(
        profile_text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN
    )

async def show_comments_menu(update, context, post_id):
    """Show comments menu for a post."""
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await update.message.reply_text("❌ Post not found.", reply_markup=main_menu)
        return
    
    # Count comments
    comments_count = db_fetch_one(
        "SELECT COUNT(*) as count FROM comments WHERE post_id = %s",
        (post_id,)
    )
    count = comments_count['count'] if comments_count else 0
    
    keyboard = [
        [
            InlineKeyboardButton(f"👁 View Comments ({count})", callback_data=f"viewcomments_{post_id}_1"),
            InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")
        ]
    ]
    
    post_preview = post['content'][:200] + '...' if len(post['content']) > 200 else post['content']
    text = f"💬 *Post Preview*\n\n{escape_markdown(post_preview, version=2)}"
    
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def show_comments_page(update, context, post_id, page=1):
    """Show comments for a post."""
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await context.bot.send_message(update.effective_chat.id, "❌ Post not found.")
        return
    
    per_page = 10
    offset = (page - 1) * per_page
    
    # Get top-level comments
    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = %s AND parent_comment_id = 0 ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (post_id, per_page, offset)
    )
    
    total_comments = db_fetch_one(
        "SELECT COUNT(*) as count FROM comments WHERE post_id = %s AND parent_comment_id = 0",
        (post_id,)
    )
    total_count = total_comments['count'] if total_comments else 0
    total_pages = (total_count + per_page - 1) // per_page
    
    if not comments and page == 1:
        text = "💬 *Comments*\n\nNo comments yet. Be the first to comment!"
        keyboard = [
            [InlineKeyboardButton("✍️ Write First Comment", callback_data=f"writecomment_{post_id}")],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
        await context.bot.send_message(
            update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    text = f"💬 *Comments* - Page {page}/{total_pages}\n\n"
    
    for comment in comments:
        author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (comment['author_id'],))
        author_name = get_display_name(author)
        author_sex = get_display_sex(author)
        
        # Use profileid_ with user_id for safe linking
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{comment['author_id']}"
        
        comment_text = escape_markdown(comment['content'], version=2)
        text += f"💬 {comment_text}\n"
        text += f"👤 [{author_name}]({profile_link}) {author_sex}\n"
        text += f"🕒 {comment['timestamp'].strftime('%b %d, %H:%M')}\n"
        
        # Add reply button
        text += f"🔹 [Reply](https://t.me/{BOT_USERNAME}?start=writecomment_{post_id}_{comment['comment_id']})\n\n"
    
    # Pagination buttons
    keyboard = []
    if page > 1:
        keyboard.append([InlineKeyboardButton("⬅️ Previous", callback_data=f"viewcomments_{post_id}_{page-1}")])
    if page < total_pages:
        keyboard.append([InlineKeyboardButton("Next ➡️", callback_data=f"viewcomments_{post_id}_{page+1}")])
    
    keyboard.append([InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")])
    keyboard.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    await context.bot.send_message(
        update.effective_chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True
    )

async def show_previous_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Show user's previous posts."""
    user_id = str(update.effective_user.id)
    
    per_page = 5
    offset = (page - 1) * per_page
    
    posts = db_fetch_all(
        "SELECT * FROM posts WHERE author_id = %s AND approved = TRUE ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    )
    
    total_posts = db_fetch_one(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
        (user_id,)
    )
    total_count = total_posts['count'] if total_posts else 0
    total_pages = (total_count + per_page - 1) // per_page
    
    if not posts:
        text = "📚 *My Previous Posts*\n\nYou haven't posted anything yet."
        keyboard = [
            [InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
    else:
        text = f"📚 *My Previous Posts* - Page {page}/{total_pages}\n\n"
        
        for post in posts:
            post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
            text += f"📄 *{post['category']}*\n"
            text += f"{escape_markdown(post_preview, version=2)}\n"
            
            # Count comments for this post
            comments_count = db_fetch_one(
                "SELECT COUNT(*) as count FROM comments WHERE post_id = %s",
                (post['post_id'],)
            )
            count = comments_count['count'] if comments_count else 0
            text += f"💬 {count} comments\n"
            text += f"🕒 {post['timestamp'].strftime('%b %d, %H:%M')}\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # Build keyboard with post actions
        keyboard = []
        for post in posts:
            keyboard.append([
                InlineKeyboardButton(f"💬 View Comments", callback_data=f"viewcomments_{post['post_id']}_1"),
                InlineKeyboardButton(f"➕ Continue", callback_data=f"continue_post_{post['post_id']}")
            ])
        
        # Pagination
        pagination_row = []
        if page > 1:
            pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"previous_posts_{page-1}"))
        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"previous_posts_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)
        
        keyboard.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None, thread_from_post_id: int = None):
    """Send post confirmation with preview."""
    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data='edit_post'),
            InlineKeyboardButton("❌ Cancel", callback_data='cancel_post')
        ],
        [
            InlineKeyboardButton("✅ Submit", callback_data='confirm_post')
        ]
    ]
    
    thread_text = ""
    if thread_from_post_id:
        thread_post = db_fetch_one("SELECT content FROM posts WHERE post_id = %s", (thread_from_post_id,))
        if thread_post:
            thread_preview = thread_post['content'][:100] + '...' if len(thread_post['content']) > 100 else thread_post['content']
            thread_text = f"🔄 *Continuing from previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
    
    preview_text = (
        f"{thread_text}📝 *Post Preview* [{category}]\n\n"
        f"{escape_markdown(post_content, version=2)}\n\n"
        f"Please confirm your post:"
    )
    
    context.user_data['pending_post'] = {
        'content': post_content,
        'category': category,
        'media_type': media_type,
        'media_id': media_id,
        'thread_from_post_id': thread_from_post_id
    }
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                preview_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                preview_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        logger.error(f"Error in send_post_confirmation: {e}")
        await update.message.reply_text("❌ Error showing confirmation. Please try again.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all button callbacks."""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    data = query.data

    try:
        if data == 'ask':
            await query.message.reply_text(
                "📚 *Choose a category:*",
                reply_markup=build_category_buttons(),
                parse_mode=ParseMode.MARKDOWN
            )

        elif data.startswith('category_'):
            category = data.split('_', 1)[1]
            db_execute(
                "UPDATE users SET waiting_for_post = TRUE, selected_category = %s WHERE user_id = %s",
                (category, user_id)
            )
            await query.message.reply_text(
                f"✍️ *Please type your thought for #{category}:*\n\nYou can also send a photo or voice message.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=True)
            )

        elif data == 'menu':
            await show_main_menu(update, context)

        elif data == 'profile':
            await show_user_profile(update, context, user_id)

        elif data == 'previous_posts':
            await show_previous_posts(update, context, 1)

        elif data.startswith('previous_posts_'):
            page = int(data.split('_')[2])
            await show_previous_posts(update, context, page)

        elif data.startswith('continue_post_'):
            post_id = int(data.split('_')[2])
            post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
            
            if post and post['author_id'] == user_id:
                context.user_data['thread_from_post_id'] = post_id
                await query.message.reply_text(
                    "📚 *Choose a category for your continuation:*",
                    reply_markup=build_category_buttons(),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.answer("❌ You can only continue your own posts", show_alert=True)

        elif data.startswith('viewcomments_'):
            parts = data.split('_')
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                post_id = int(parts[1])
                page = int(parts[2])
                await show_comments_page(update, context, post_id, page)

        elif data.startswith('writecomment_'):
            post_id_str = data.split('_', 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s",
                    (post_id, user_id)
                )
                
                await query.message.reply_text(
                    "✍️ Please type your comment:",
                    reply_markup=ForceReply(selective=True)
                )

        elif data in ('edit_post', 'cancel_post', 'confirm_post'):
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.edit_message_text("❌ Post data not found. Please start over.")
                return
            
            if data == 'edit_post':
                await query.edit_message_text(
                    "✏️ Please edit your post:",
                    reply_markup=ForceReply(selective=True)
                )
                return
            
            elif data == 'cancel_post':
                await query.edit_message_text("❌ Post cancelled.")
                context.user_data.pop('pending_post', None)
                context.user_data.pop('thread_from_post_id', None)
                return
            
            elif data == 'confirm_post':
                category = pending_post['category']
                post_content = pending_post['content']
                media_type = pending_post.get('media_type', 'text')
                media_id = pending_post.get('media_id')
                thread_from_post_id = pending_post.get('thread_from_post_id')
                
                # Insert post
                if thread_from_post_id:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, category, media_type, media_id, thread_from_post_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, category, media_type, media_id, thread_from_post_id),
                        fetchone=True
                    )
                else:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, category, media_type, media_id) VALUES (%s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, category, media_type, media_id),
                        fetchone=True
                    )
                
                # Clean up
                context.user_data.pop('pending_post', None)
                context.user_data.pop('thread_from_post_id', None)
                
                if post_row:
                    await query.edit_message_text(
                        "✅ Your post has been submitted for admin approval!\n"
                        "You'll be notified when it's published."
                    )
                    
                    # Notify admin
                    if ADMIN_ID:
                        post_preview = post_content[:100] + '...' if len(post_content) > 100 else post_content
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post_row['post_id']}"),
                                InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post_row['post_id']}")
                            ]
                        ])
                        
                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=f"🆕 New post from {get_display_name(db_fetch_one('SELECT * FROM users WHERE user_id = %s', (user_id,)))}:\n\n{post_preview}",
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Error notifying admin: {e}")
                else:
                    await query.edit_message_text("❌ Failed to submit post. Please try again.")

        elif data.startswith('approve_post_'):
            # Admin approval logic
            post_id = int(data.split('_')[2])
            await approve_post(update, context, post_id)

        elif data.startswith('reject_post_'):
            # Admin rejection logic
            post_id = int(data.split('_')[2])
            await reject_post(update, context, post_id)

        elif data == 'leaderboard':
            await show_leaderboard(update, context)

        elif data == 'settings':
            await show_settings(update, context)

        elif data == 'help':
            help_text = (
                "ℹ️ *How to Use This Bot:*\n\n"
                "• Use 'Share My Thoughts' to post anonymously\n"
                "• Choose a category and write your post\n"
                "• View and continue your previous posts\n"
                "• Comment on other posts\n"
                "• Customize your profile name and settings\n"
                "• Follow other users and send private messages"
            )
            await query.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        await query.message.reply_text("❌ An error occurred. Please try again.")

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show leaderboard."""
    top_users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
        LIMIT 10
    ''')
    
    if not top_users:
        await update.message.reply_text("🏆 *Leaderboard*\n\nNo users yet.")
        return
    
    text = "🏆 *Top Contributors* 🏆\n\n"
    for idx, user in enumerate(top_users, 1):
        stars = format_stars(user['total'] // 5)
        text += f"{idx}. {user['anonymous_name']} {user['sex']} - {user['total']} contributions {stars}\n"
    
    keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu."""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT notifications_enabled, privacy_public FROM users WHERE user_id = %s", (user_id,))
    
    if not user:
        await update.message.reply_text("Please use /start first.")
        return
    
    notifications_status = "✅ ON" if user['notifications_enabled'] else "❌ OFF"
    privacy_status = "🌍 Public" if user['privacy_public'] else "🔒 Private"
    
    keyboard = [
        [InlineKeyboardButton(f"🔔 Notifications: {notifications_status}", callback_data='toggle_notifications')],
        [InlineKeyboardButton(f"👁‍🗨 Privacy: {privacy_status}", callback_data='toggle_privacy')],
        [InlineKeyboardButton("✏️ Change Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Change Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    
    text = "⚙️ *Settings*\n\nManage your preferences:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Approve and publish a post."""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Check if user is admin
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("❌ You don't have permission to do this.", show_alert=True)
        return
    
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await query.answer("❌ Post not found.", show_alert=True)
        return
    
    try:
        # Format post for channel
        hashtag = f"#{post['category']}"
        caption_text = (
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
        # Create comments button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Send to channel
        if post['media_type'] == 'text':
            message = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
        else:
            # Handle media posts
            message = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
        
        # Update post in database
        db_execute(
            "UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s WHERE post_id = %s",
            (user_id, message.message_id, post_id)
        )
        
        # Notify author
        try:
            await context.bot.send_message(
                chat_id=post['author_id'],
                text="✅ Your post has been approved and published!"
            )
        except Exception as e:
            logger.error(f"Error notifying author: {e}")
        
        await query.edit_message_text("✅ Post approved and published!")
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        await query.answer("❌ Failed to approve post.", show_alert=True)

async def reject_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Reject a post."""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Check if user is admin
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("❌ You don't have permission to do this.", show_alert=True)
        return
    
    # Delete post
    db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
    
    # Notify author
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if post:
        try:
            await context.bot.send_message(
                chat_id=post['author_id'],
                text="❌ Your post was not approved by the admin."
            )
        except Exception as e:
            logger.error(f"Error notifying author: {e}")
    
    await query.edit_message_text("❌ Post rejected and deleted.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming messages."""
    user_id = str(update.effective_user.id)
    text = update.message.text or update.message.caption or ""
    
    # Initialize user if not exists
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        anon_name = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        db_execute(
            "INSERT INTO users (user_id, anonymous_name, is_admin) VALUES (%s, %s, %s)",
            (user_id, anon_name, is_admin)
        )
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # Handle post creation
    if user and user['waiting_for_post']:
        category = user['selected_category']
        db_execute(
            "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
            (user_id,)
        )
        
        post_content = text
        media_type = 'text'
        media_id = None
        
        # Handle media
        if update.message.photo:
            photo = update.message.photo[-1]
            media_id = photo.file_id
            media_type = 'photo'
        elif update.message.voice:
            voice = update.message.voice
            media_id = voice.file_id
            media_type = 'voice'
        
        thread_from_post_id = context.user_data.get('thread_from_post_id')
        await send_post_confirmation(update, context, post_content, category, media_type, media_id, thread_from_post_id)
        return

    # Handle comment creation
    elif user and user['waiting_for_comment']:
        post_id = user['comment_post_id']
        db_execute(
            "UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL WHERE user_id = %s",
            (user_id,)
        )
        
        # Insert comment
        db_execute(
            "INSERT INTO comments (post_id, author_id, content) VALUES (%s, %s, %s)",
            (post_id, user_id, text)
        )
        
        await update.message.reply_text("✅ Comment posted!", reply_markup=main_menu)
        return

    # Handle name change
    elif user and user['awaiting_name']:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s",
                (new_name, user_id)
            )
            await update.message.reply_text(f"✅ Name updated to {new_name}!")
        else:
            await update.message.reply_text("❌ Name must be 1-30 characters.")
        return

    # Handle main menu buttons
    elif text == "🌟 Share My Thoughts":
        await update.message.reply_text(
            "📚 *Choose a category:*",
            reply_markup=build_category_buttons(),
            parse_mode=ParseMode.MARKDOWN
        )

    elif text == "👤 View Profile":
        await show_user_profile(update, context, user_id)

    elif text == "📚 My Previous Posts":
        await show_previous_posts(update, context, 1)

    elif text == "🏆 Leaderboard":
        await show_leaderboard(update, context)

    elif text == "⚙️ Settings":
        await show_settings(update, context)

    elif text == "❓ Help":
        await update.message.reply_text(
            "Send /start to see the main menu or use the buttons below.",
            reply_markup=main_menu
        )

    else:
        # Default response
        await update.message.reply_text(
            "How can I help you? Use the buttons below:",
            reply_markup=main_menu
        )

async def error_handler(update, context):
    """Handle errors."""
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True)

def main():
    """Main function to start the bot."""
    # Initialize database
    if not init_db_pool():
        logger.error("Failed to initialize database pool")
        return
    
    if not init_database():
        logger.error("Failed to initialize database tables")
        return
    
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", show_main_menu))
    application.add_handler(CommandHandler("leaderboard", show_leaderboard))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    # Start Flask server in background
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    # Start bot
    logger.info("Bot starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
