# bot.py
import os
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Load environment variables
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMIN_ID = os.getenv("ADMIN_ID")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Database pool ----------
try:
    db_pool = pool.SimpleConnectionPool(
        minconn=1, maxconn=10, dsn=DATABASE_URL, cursor_factory=RealDictCursor
    )
    logger.info("DB pool created")
except Exception as e:
    logger.error("Failed to create DB pool: %s", e)
    db_pool = None


def db_execute(query, params=(), fetch=False, fetchone=False):
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
        logger.error("Database error: %s\nQuery: %s\nParams: %s", e, query, params)
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


# ---------- Initialize DB (create tables + migrations) ----------
def init_db():
    try:
        # create core tables and ensure missing columns are added
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
                reply_idx INTEGER,
                nested_idx INTEGER,
                notifications_enabled BOOLEAN DEFAULT TRUE,
                privacy_public BOOLEAN DEFAULT TRUE,
                is_admin BOOLEAN DEFAULT FALSE,
                waiting_for_private_message BOOLEAN DEFAULT FALSE,
                private_message_target TEXT,
                continuation_post_id INTEGER DEFAULT NULL
            )
        ''')

        db_execute('''
            CREATE TABLE IF NOT EXISTS followers (
                follower_id TEXT,
                followed_id TEXT,
                PRIMARY KEY (follower_id, followed_id)
            )
        ''')

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

        db_execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                reaction_id SERIAL PRIMARY KEY,
                comment_id INTEGER REFERENCES comments(comment_id),
                user_id TEXT,
                type TEXT,
                UNIQUE(comment_id, user_id)
            )
        ''')

        db_execute('''
            CREATE TABLE IF NOT EXISTS private_messages (
                message_id SERIAL PRIMARY KEY,
                sender_id TEXT REFERENCES users(user_id),
                receiver_id TEXT REFERENCES users(user_id),
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE
            )
        ''')

        db_execute('''
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id TEXT REFERENCES users(user_id),
                blocked_id TEXT REFERENCES users(user_id),
                PRIMARY KEY (blocker_id, blocked_id)
            )
        ''')

        # create admin user if provided
        if ADMIN_ID:
            db_execute('''
                INSERT INTO users (user_id, anonymous_name, is_admin)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE
            ''', (str(ADMIN_ID), "Admin"))

        # Defensive migrations (safe if column already exists)
        # Note: Postgres supports ADD COLUMN IF NOT EXISTS
        db_execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS thread_from_post_id BIGINT DEFAULT NULL")
        db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS continuation_post_id INTEGER DEFAULT NULL")

        logger.info("Database initialized & migrations run successfully")
    except Exception as e:
        logger.error("init_db error: %s", e)


# ---------- Utility helpers ----------
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


def create_anonymous_name(user_id):
    try:
        uid_int = int(user_id)
    except Exception:
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
    if rating <= 0:
        return ""
    full_stars = min(rating // 5, max_stars)
    empty_stars = max(0, max_stars - full_stars)
    return '⭐️' * full_stars + '☆' * empty_stars


def get_display_name(user_data):
    if user_data and user_data.get('anonymous_name'):
        return user_data['anonymous_name']
    return "Anonymous"


def get_display_sex(user_data):
    if user_data and user_data.get('sex'):
        return user_data['sex']
    return '👤'


def count_all_comments(post_id):
    # recursively count top-level comments and replies
    def count_replies(parent_id):
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
        total = len(comments) if comments else 0
        for c in comments or []:
            total += count_replies(c['comment_id'])
        return total
    return count_replies(None)


# ---------- Telegram actions & UI ----------

# Attractive "My Vent" label
MY_VENT_LABEL = "🤍 Vent Here"
MY_PREV_POSTS_LABEL = "📚 My Previous Posts"


def main_menu_markup():
    keyboard = [
        [InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask')],
        [InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'),
         InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("❓ Help", callback_data='help'),
         InlineKeyboardButton("ℹ️ About Us", callback_data='about')],
    ]
    return InlineKeyboardMarkup(keyboard)


def post_action_buttons(post_id):
    # "View Comments" (keeps the t.me deep link style you had)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Comments", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton(MY_VENT_LABEL, callback_data='ask')]
    ])


# ---------- Bot command handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    args = context.args or []

    # ensure user exists
    existing = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not existing:
        anon = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        db_execute(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        )

    if args:
        arg = args[0]
        if arg.startswith("comments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                await show_comments_menu(update, context, int(post_id_str), page=1)
                return
        elif arg.startswith("viewcomments_"):
            parts = arg.split("_")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                await show_comments_page(update, context, int(parts[1]), int(parts[2]))
                return
        elif arg.startswith("profileid_"):
            # profile by user_id (robust, no encoding issues)
            target_user_id = arg.split("_", 1)[1]
            await render_profile_for_user(starting_update=update, context=context, target_user_id=target_user_id)
            return

    # default welcome
    try:
        await update.message.reply_text(
            "🌟✝️ *Welcome to Christian vent* ✝️🌟\n\n"
            "Share your thoughts anonymously and support each other.\n",
            reply_markup=main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning("Could not send welcome via message: %s", e)
        if update.callback_query:
            await update.callback_query.message.reply_text(
                "🌟✝️ *Welcome to Christian vent* ✝️🌟\n\n"
                "Share your thoughts anonymously and support each other.\n",
                reply_markup=main_menu_markup(),
                parse_mode=ParseMode.MARKDOWN
            )


async def render_profile_for_user(starting_update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: str):
    # Fetch user by id, then render profile text & actions
    user_row = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (target_user_id,))
    if not user_row:
        if starting_update.message:
            await starting_update.message.reply_text("❌ User not found.")
        elif starting_update.callback_query:
            await starting_update.callback_query.message.reply_text("❌ User not found.")
        return

    display_name = get_display_name(user_row)
    display_sex = get_display_sex(user_row)
    followers = db_fetch_all("SELECT * FROM followers WHERE followed_id = %s", (target_user_id,)) or []
    rating = calculate_user_rating(target_user_id)
    stars = format_stars(rating)

    # Buttons: follow/unfollow for others, My Previous Posts for self, message for others
    buttons = []
    current_user_id = str(starting_update.effective_user.id)
    if current_user_id != target_user_id:
        is_following = db_fetch_one(
            "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
            (current_user_id, target_user_id)
        )
        if is_following:
            buttons.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{target_user_id}')])
            buttons.append([InlineKeyboardButton("✉️ Send Message", callback_data=f'message_{target_user_id}')])
        else:
            buttons.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{target_user_id}')])
    else:
        # For user's own profile add "My Previous Posts"
        buttons.append([InlineKeyboardButton(MY_PREV_POSTS_LABEL, callback_data='previous_posts')])

    buttons.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])

    send_text = (
        f"👤 *{display_name}* 🎖\n"
        f"📌 Sex: {display_sex}\n\n"
        f"👥 Followers: {len(followers)}\n"
        f"⭐️ Contributions: {rating} {stars}\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"_Use /menu to return_"
    )

    try:
        if starting_update.message:
            await starting_update.message.reply_text(
                send_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        elif starting_update.callback_query:
            await starting_update.callback_query.message.reply_text(
                send_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    except Exception as e:
        logger.error("Error rendering profile: %s", e)
        try:
            await starting_update.message.reply_text("❌ Error loading profile.")
        except Exception:
            pass


async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) +
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
        LIMIT 10
    ''') or []

    text = "🏆 *Top Contributors* 🏆\n\n"
    for idx, u in enumerate(users, 1):
        stars = format_stars(u['total'] // 5)
        text += f"{idx}. {u['anonymous_name']} {u['sex']} - {u['total']} contributions {stars}\n"

    try:
        if update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]))
        elif update.callback_query:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]))
    except Exception as e:
        logger.error("show_leaderboard error: %s", e)


async def show_comments_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int, page=1):
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await update.message.reply_text("❌ Post not found.")
        return

    comment_count = count_all_comments(post_id)
    keyboard = [
        [InlineKeyboardButton(f"👁 View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_{page}")],
        [InlineKeyboardButton(MY_VENT_LABEL, callback_data='ask')],
    ]
    escaped_text = (post['content'][:800] + '...') if post['content'] and len(post['content']) > 800 else (post['content'] or "")
    await update.message.reply_text(f"💬\n{escaped_text}", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_comments_page(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int, page=1):
    # Send only comments (not re-sending post)
    chat_id = update.effective_chat.id
    per_page = 6
    offset = (page - 1) * per_page

    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = %s AND parent_comment_id = 0 ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (post_id, per_page, offset)
    ) or []

    total_comments = count_all_comments(post_id)
    total_pages = max(1, (total_comments + per_page - 1) // per_page)

    header = "💬 *Comments*\n\n"

    if not comments and page == 1:
        await context.bot.send_message(chat_id, header + "_No comments yet._", parse_mode=ParseMode.MARKDOWN)
        return

    header_msg = await context.bot.send_message(chat_id, header, parse_mode=ParseMode.MARKDOWN)
    header_message_id = header_msg.message_id
    user_id = str(update.effective_user.id)

    # For each top-level comment
    for comment in comments:
        commenter_id = comment['author_id']
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (commenter_id,))
        display_name = get_display_name(commenter)
        display_sex = get_display_sex(commenter)
        rating = calculate_user_rating(commenter_id)
        stars = format_stars(rating)

        # Build profile link using user_id (robust)
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{commenter_id}"
        author_text = f"[{display_name}]({profile_link}) {display_sex} {stars}"

        # reaction counts
        likes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'", (comment['comment_id'],)) or {'cnt': 0}
        dislikes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'", (comment['comment_id'],)) or {'cnt': 0}
        likes = likes_row['cnt']
        dislikes = dislikes_row['cnt']

        # keyboard
        kb_buttons = [
            [InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{comment['comment_id']}"),
             InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{comment['comment_id']}"),
             InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")]
        ]
        if comment['author_id'] == user_id:
            kb_buttons.append([
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment['comment_id']}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment['comment_id']}")
            ])
        kb = InlineKeyboardMarkup(kb_buttons)

        # send comment (Markdown link uses parse_mode=MARKDOWN)
        safe_comment = comment['content'] or ""
        try:
            await context.bot.send_message(
                chat_id,
                text=f"{safe_comment}\n\n{author_text}",
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=header_message_id,
                disable_web_page_preview=True
            )
        except BadRequest as e:
            # If Markdown link fails due to characters, fallback to plain text + separate button that links to profile
            logger.warning("Markdown link send failed, falling back: %s", e)
            fallback_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 View Profile", url=profile_link),
                 InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")]
            ])
            await context.bot.send_message(
                chat_id,
                text=f"{safe_comment}\n\n{display_name} {display_sex} {stars}",
                reply_markup=fallback_kb,
                reply_to_message_id=header_message_id
            )

        # display replies recursively (limited depth)
        async def send_replies_recursive(parent_comment_id, parent_msg_id, depth=1, max_depth=5):
            if depth > max_depth:
                return
            children = db_fetch_all("SELECT * FROM comments WHERE parent_comment_id = %s ORDER BY timestamp", (parent_comment_id,)) or []
            for child in children:
                reply_user_id = child['author_id']
                reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (reply_user_id,))
                reply_display_name = get_display_name(reply_user)
                reply_display_sex = get_display_sex(reply_user)
                rating_reply = calculate_user_rating(reply_user_id)
                stars_reply = format_stars(rating_reply)
                profile_link_reply = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply_user_id}"
                reply_text = f"[{reply_display_name}]({profile_link_reply}) {reply_display_sex} {stars_reply}"
                reply_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👍", callback_data=f"likereply_{child['comment_id']}"),
                     InlineKeyboardButton("👎", callback_data=f"dislikereply_{child['comment_id']}"),
                     InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{child['comment_id']}")]
                ])
                try:
                    child_msg = await context.bot.send_message(
                        chat_id,
                        text=f"{child['content']}\n\n{reply_text}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_to_message_id=parent_msg_id,
                        reply_markup=reply_kb,
                        disable_web_page_preview=True
                    )
                except BadRequest:
                    # fallback
                    await context.bot.send_message(
                        chat_id,
                        text=f"{child['content']}\n\n{reply_display_name} {reply_display_sex} {stars_reply}",
                        reply_to_message_id=parent_msg_id,
                        reply_markup=reply_kb
                    )
                    child_msg = None
                # recursive
                next_parent_id = child['comment_id']
                next_parent_msg_id = child_msg.message_id if child_msg else parent_msg_id
                await send_replies_recursive(next_parent_id, next_parent_msg_id, depth + 1)

        # we don't have the message id returned consistently for top-level comment (send_message returns it),
        # but for simplicity we don't rely on it for replies here (Telegram may thread them anyway).
        # Send replies with reply_to_message_id header_message_id so they appear nested enough.
        await send_replies_recursive(comment['comment_id'], header_message_id, depth=1, max_depth=4)

    # Pagination
    pagination = []
    if page > 1:
        pagination.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"viewcomments_{post_id}_{page-1}"))
    if page < total_pages:
        pagination.append(InlineKeyboardButton("Next ➡️", callback_data=f"viewcomments_{post_id}_{page+1}"))
    if pagination:
        await context.bot.send_message(chat_id, text=f"📄 Page {page}/{total_pages}", reply_markup=InlineKeyboardMarkup([pagination]))


# ---------- Message handling for creating posts / comments / continuations ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text or ""
    # normalize
    text_stripped = text.strip()

    # check states in DB user row
    user_row = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user_row:
        anon = create_anonymous_name(user_id)
        db_execute("INSERT INTO users (user_id, anonymous_name) VALUES (%s, %s)", (user_id, anon))
        user_row = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # 1) awaiting name change
    if user_row.get('awaiting_name'):
        new_name = text_stripped[:50]
        db_execute("UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s", (new_name, user_id))
        await update.message.reply_text(f"✅ Name updated to *{new_name}*", parse_mode=ParseMode.MARKDOWN)
        return

    # 2) continuing a previous post (we store continuation_post_id in users table)
    if user_row.get('continuation_post_id'):
        cont_post_id = user_row['continuation_post_id']
        # create new post that references thread_from_post_id = cont_post_id
        # For continuations we will publish directly (approved TRUE). Change as needed.
        created = db_execute(
            "INSERT INTO posts (content, author_id, category, media_type, approved, thread_from_post_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING post_id",
            (text_stripped, user_id, "Continuation", "text", True, cont_post_id),
            fetchone=True
        )
        # clear continuation
        db_execute("UPDATE users SET continuation_post_id = NULL WHERE user_id = %s", (user_id,))
        if created and isinstance(created, dict) and created.get('post_id'):
            new_id = created['post_id']
            await update.message.reply_text("✅ Your continuation was posted successfully!")
            # update channel (send to channel) if you want the continuation to be posted to channel
            try:
                channel_text = f"{text_stripped}\n\n🔁 Continued from post #{cont_post_id}\n[Bot](https://t.me/{BOT_USERNAME})"
                msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=channel_text, parse_mode=ParseMode.MARKDOWN)
                # store channel_message_id
                db_execute("UPDATE posts SET channel_message_id = %s WHERE post_id = %s", (msg.message_id, new_id))
            except Exception as e:
                logger.error("Failed to publish continuation to channel: %s", e)
        else:
            await update.message.reply_text("❌ Failed to save your continuation. Please try again.")
        return

    # 3) posting a new post (ask flow sets waiting_for_post)
    if user_row.get('waiting_for_post'):
        category = user_row.get('selected_category') or "Other"
        # assume immediate approval False; adapt if you want admin approval
        created = db_execute(
            "INSERT INTO posts (content, author_id, category, media_type, approved) VALUES (%s, %s, %s, %s, %s) RETURNING post_id",
            (text_stripped, user_id, category, "text", False),
            fetchone=True
        )
        db_execute("UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s", (user_id,))
        if created and isinstance(created, dict) and created.get('post_id'):
            post_id = created['post_id']
            await update.message.reply_text("✅ Your post was submitted for review. Admin will approve it soon.")
            # notify admin
            if ADMIN_ID:
                try:
                    preview = text_stripped[:100] + '...' if len(text_stripped) > 100 else text_stripped
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post_id}"),
                                                InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post_id}")]])
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"New post pending approval:\n\n{preview}", reply_markup=kb)
                except Exception as e:
                    logger.error("Failed to notify admin: %s", e)
        else:
            await update.message.reply_text("❌ Failed to submit post. Try again.")
        return

    # 4) writing a comment flow if waiting_for_comment
    if user_row.get('waiting_for_comment') and user_row.get('comment_post_id'):
        target_post_id = user_row['comment_post_id']
        created = db_execute(
            "INSERT INTO comments (post_id, parent_comment_id, author_id, content) VALUES (%s, %s, %s, %s) RETURNING comment_id",
            (target_post_id, 0, user_id, text_stripped),
            fetchone=True
        )
        db_execute("UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL WHERE user_id = %s", (user_id,))
        if created:
            await update.message.reply_text("✅ Comment posted!")
            # Optionally notify post author
            post = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (target_post_id,))
            if post and post.get('author_id'):
                try:
                    await context.bot.send_message(chat_id=post['author_id'], text=f"💬 Someone commented on your post (#{target_post_id}).")
                except Exception as e:
                    logger.debug("Could not notify post author: %s", e)
        else:
            await update.message.reply_text("❌ Failed to post comment. Try again.")
        return

    # default: not in any special flow
    await update.message.reply_text("I didn't understand that. Use the menu or press 🤍 Vent Here to create a post.", reply_markup=main_menu_markup())


# ---------- Callback button handler ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)

    try:
        if data == 'ask':
            # start new post flow: ask for category
            await query.message.reply_text("📚 *Choose a category:*", reply_markup=build_category_buttons(), parse_mode=ParseMode.MARKDOWN)
            return

        if data.startswith('category_'):
            category = data.split('_', 1)[1]
            db_execute("UPDATE users SET waiting_for_post = TRUE, selected_category = %s WHERE user_id = %s", (category, user_id))
            await query.message.reply_text(f"✍️ Please type your thought for #{category} (send text).", parse_mode=ParseMode.MARKDOWN, reply_markup=ForceReply(selective=True))
            return

        if data == 'menu':
            try:
                await query.message.edit_text("📱 Main Menu", reply_markup=main_menu_markup())
            except BadRequest:
                await query.message.reply_text("📱 Main Menu", reply_markup=main_menu_markup())
            return

        if data == 'profile':
            await render_profile_for_user(starting_update=update, context=context, target_user_id=user_id)
            return

        if data == 'leaderboard':
            await show_leaderboard(update, context)
            return

        if data == 'settings':
            await show_settings(update, context)
            return

        if data == 'help':
            await query.message.reply_text("ℹ️ Help: Use the menu to navigate. Press 🤍 Vent Here to create a post.")
            return

        if data == 'about':
            await query.message.reply_text("👤 Creator: Yididiya Tamiru\n🙏 Christian vent: share anonymously.")
            return

        if data.startswith('approve_post_'):
            # only admin
            if str(query.from_user.id) != str(ADMIN_ID):
                await query.answer("You are not allowed.", show_alert=True)
                return
            post_id = int(data.split('_')[-1])
            post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
            if not post:
                await query.message.reply_text("Post not found.")
                return
            # publish to channel
            caption = f"{post['content']}\n\n━━━━━━━━\n#{post['category']}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]])
            if post['media_type'] == 'text':
                msg = await context.bot.send_message(CHANNEL_ID, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            else:
                msg = await context.bot.send_message(CHANNEL_ID, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            # update DB
            db_execute("UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s WHERE post_id = %s", (str(query.from_user.id), msg.message_id, post_id))
            await query.message.edit_text("✅ Post approved & published.")
            return

        if data.startswith('reject_post_'):
            if str(query.from_user.id) != str(ADMIN_ID):
                await query.answer("You are not allowed.", show_alert=True)
                return
            post_id = int(data.split('_')[-1])
            db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
            await query.message.reply_text("❌ Post rejected and deleted.")
            return

        if data.startswith('viewcomments_'):
            parts = data.split('_')
            if len(parts) >= 3:
                pid = int(parts[1])
                page = int(parts[2])
                await show_comments_page(update, context, pid, page)
            return

        # Follow/unfollow
        if data.startswith('follow_') or data.startswith('unfollow_'):
            target_uid = data.split('_', 1)[1]
            if data.startswith('follow_'):
                try:
                    db_execute("INSERT INTO followers (follower_id, followed_id) VALUES (%s, %s)", (user_id, target_uid))
                except Exception:
                    pass
                await query.message.reply_text("✅ Followed.")
            else:
                db_execute("DELETE FROM followers WHERE follower_id = %s AND followed_id = %s", (user_id, target_uid))
                await query.message.reply_text("✅ Unfollowed.")
            return

        # View previous posts (for current user)
        if data == 'previous_posts':
            await show_previous_posts(update, context, page=1)
            return

        # When user selects one of their previous posts to Continue
        if data.startswith('continue_post_'):
            post_id = int(data.split('_')[-1])
            # set continuation_post_id in users table and prompt user to send continuation
            db_execute("UPDATE users SET continuation_post_id = %s WHERE user_id = %s", (post_id, user_id))
            await query.message.reply_text("✍️ Please type the next part of your post/story now. When you send it, it will be saved as a continuation.")
            return

        # view profile links by profileid_ handled in start via /start links; also support callback to view another user's profile
        if data.startswith('view_profile_'):
            target_uid = data.split('_', 1)[1]
            await render_profile_for_user(starting_update=update, context=context, target_user_id=target_uid)
            return

        # Comments reactions, reply flows, edit/delete etc. minimal implementations:
        if data.startswith('reply_'):
            parts = data.split('_')
            if len(parts) >= 3:
                post_id = int(parts[1])
                parent_comment_id = int(parts[2])
                # set waiting_for_comment and comment_post_id and reply_idx if you want to track parent
                db_execute("UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s", (post_id, user_id))
                await query.message.reply_text("✍️ Please type your reply. It will be posted under the comment.")
            return

        if data.startswith('likecomment_') or data.startswith('dislikecomment_'):
            parts = data.split('_')
            c_id = int(parts[1])
            reaction = 'like' if data.startswith('likecomment_') else 'dislike'
            # remove existing then insert new
            db_execute("DELETE FROM reactions WHERE comment_id = %s AND user_id = %s", (c_id, user_id))
            db_execute("INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)", (c_id, user_id, reaction))
            await query.answer("Reaction updated")
            return

        # open "My Vent" (ask)
        if data == 'ask':
            # we already handled category_ above, default fallback:
            await query.message.reply_text("📚 Choose a category:", reply_markup=build_category_buttons())
            return

    except Exception as e:
        logger.error("button_handler exception: %s", e)
        try:
            await query.answer("❌ Error handling action", show_alert=True)
        except Exception:
            pass


# ---------- Additional UI: show_previous_posts ----------
async def show_previous_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_id = str(update.effective_user.id)
    per_page = 6
    offset = (page - 1) * per_page
    posts = db_fetch_all(
        "SELECT * FROM posts WHERE author_id = %s ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    ) or []
    total_row = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE author_id = %s", (user_id,))
    total = total_row['count'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    if not posts:
        await update.callback_query.message.reply_text("📝 You have no previous posts.")
        return

    text = f"📚 *Your Posts* (Page {page}/{total_pages})\n\n"
    kb = []
    for p in posts:
        preview = p['content'][:40] + ('...' if len(p['content']) > 40 else '')
        text += f"• {preview} (#{p['post_id']})\n"
        kb.append([InlineKeyboardButton(f"{preview}", callback_data=f"viewpost_{p['post_id']}")])
        kb.append([InlineKeyboardButton("➕ Continue This Post", callback_data=f"continue_post_{p['post_id']}")])

    # pagination
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"previous_posts_page_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"previous_posts_page_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])

    try:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        try:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error("show_previous_posts send failed: %s", e)


# handle pagination callback generated above
async def previous_posts_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # matches callback data previous_posts_page_<n>
    data = update.callback_query.data
    try:
        page = int(data.split('_')[-1])
    except Exception:
        page = 1
    await show_previous_posts(update, context, page=page)


# ---------- Settings ----------

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user:
        await update.callback_query.message.reply_text("Please start /start first.")
        return
    notifications_status = "✅ ON" if user['notifications_enabled'] else "❌ OFF"
    privacy_status = "🌍 Public" if user['privacy_public'] else "🔒 Private"
    keyboard = [
        [InlineKeyboardButton(f"🔔 Notifications: {notifications_status}", callback_data='toggle_notifications')],
        [InlineKeyboardButton(f"👁 Privacy: {privacy_status}", callback_data='toggle_privacy')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    await update.callback_query.message.reply_text("⚙️ Settings", reply_markup=InlineKeyboardMarkup(keyboard))


# Toggle handlers
async def toggle_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cur = db_fetch_one("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,))
    if cur:
        new = not cur['notifications_enabled']
        db_execute("UPDATE users SET notifications_enabled = %s WHERE user_id = %s", (new, user_id))
    await show_settings(update, context)


async def toggle_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cur = db_fetch_one("SELECT privacy_public FROM users WHERE user_id = %s", (user_id,))
    if cur:
        new = not cur['privacy_public']
        db_execute("UPDATE users SET privacy_public = %s WHERE user_id = %s", (new, user_id))
    await show_settings(update, context)


# ---------- Entry point ----------
def main():
    init_db()
    if not TOKEN:
        logger.error("TOKEN not provided. Set TOKEN env var.")
        return

    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))

    # CallbackQuery handler (single entry)
    application.add_handler(CallbackQueryHandler(button_handler))

    # Message handler for text flows
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    # Additional small handlers for toggle callbacks
    application.add_handler(CallbackQueryHandler(toggle_notifications, pattern=r'^toggle_notifications$'))
    application.add_handler(CallbackQueryHandler(toggle_privacy, pattern=r'^toggle_privacy$'))

    # previous posts pagination
    application.add_handler(CallbackQueryHandler(previous_posts_page_handler, pattern=r'^previous_posts_page_'))

    # run the bot
    logger.info("Starting bot...")
    application.run_polling()


if __name__ == '__main__':
    main()
