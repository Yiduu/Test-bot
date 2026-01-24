# mini_app.py - Complete Christian Vent Mini App
import os
import json
import jwt
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template, send_from_directory
from functools import wraps
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Configuration from environment
DATABASE_URL = os.getenv('DATABASE_URL')
BOT_USERNAME = os.getenv('BOT_USERNAME')
SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')
APP_NAME = os.getenv('APP_NAME', 'Christian Vent')
PRIMARY_COLOR = os.getenv('PRIMARY_COLOR', '#BF970B')
BACKGROUND_COLOR = os.getenv('BACKGROUND_COLOR', '#272F32')
TEXT_COLOR = os.getenv('TEXT_COLOR', '#E0E0E0')

# Initialize Flask app
mini_app = Flask(__name__, 
                 template_folder='templates',
                 static_folder='static')
mini_app.secret_key = SECRET_KEY

# Database connection
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def db_fetch_one(query, params=()):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchone()
        return result
    finally:
        conn.close()

def db_fetch_all(query, params=()):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchall()
        return result
    finally:
        conn.close()

def db_execute(query, params=()):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()
        return True
    except Exception as e:
        print(f"Database error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

# ==================== JWT AUTHENTICATION ====================
def generate_token(user_id):
    """Generate JWT token for authentication"""
    payload = {
        'user_id': user_id,
        'exp': datetime.now(timezone.utc) + timedelta(hours=24)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')

def verify_token(token):
    """Verify JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.args.get('token') or request.headers.get('Authorization')
        
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        
        if token.startswith('Bearer '):
            token = token[7:]
        
        payload = verify_token(token)
        if not payload:
            return jsonify({'error': 'Invalid or expired token'}), 401
        
        request.user_id = payload['user_id']
        return f(*args, **kwargs)
    return decorated

# ==================== HELPER FUNCTIONS ====================
def calculate_user_rating(user_id):
    """Calculate user rating points"""
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

def format_aura(rating):
    """Create aura based on contribution points"""
    if rating >= 100:
        return "🟣"
    elif rating >= 50:
        return "🔵"
    elif rating >= 25:
        return "🟢"
    elif rating >= 10:
        return "🟡"
    else:
        return "⚪"

def get_display_name(user_data):
    if user_data and user_data.get('anonymous_name'):
        return user_data['anonymous_name']
    return "Anonymous"

# ==================== ROUTES ====================

@mini_app.route('/')
def index():
    """Main mini app page"""
    return render_template('index.html',
                         bot_username=BOT_USERNAME,
                         app_name=APP_NAME,
                         primary_color=PRIMARY_COLOR,
                         background_color=BACKGROUND_COLOR,
                         text_color=TEXT_COLOR)

@mini_app.route('/api/auth', methods=['POST'])
def authenticate():
    """Authenticate user via Telegram WebApp or manual login"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            return jsonify({'error': 'User ID required'}), 400
        
        # Check if user exists in database
        user = db_fetch_one("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'error': 'User not found. Please use the bot first.'}), 404
        
        # Generate token
        token = generate_token(user_id)
        
        # Get user data
        user_data = db_fetch_one(
            "SELECT user_id, anonymous_name, sex FROM users WHERE user_id = %s",
            (user_id,)
        )
        
        rating = calculate_user_rating(user_id)
        
        return jsonify({
            'success': True,
            'token': token,
            'user': {
                'id': user_data['user_id'],
                'name': user_data['anonymous_name'],
                'sex': user_data['sex'],
                'rating': rating,
                'aura': format_aura(rating)
            },
            'expires_in': 86400
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/feed')
@token_required
def get_feed():
    """Get approved posts for the feed"""
    try:
        page = int(request.args.get('page', 1))
        per_page = 10
        offset = (page - 1) * per_page
        
        # Get approved posts
        posts = db_fetch_all('''
            SELECT 
                p.post_id,
                p.content,
                p.category,
                p.timestamp,
                p.comment_count,
                p.media_type,
                u.anonymous_name as author_name,
                u.sex as author_sex
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            WHERE p.approved = TRUE
            ORDER BY p.timestamp DESC
            LIMIT %s OFFSET %s
        ''', (per_page, offset))
        
        # Format posts
        formatted_posts = []
        for post in posts:
            # Format timestamp
            if isinstance(post['timestamp'], str):
                post_time = datetime.strptime(post['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                post_time = post['timestamp']
            
            now = datetime.now()
            time_diff = now - post_time
            
            if time_diff.days > 0:
                time_ago = f"{time_diff.days}d ago"
            elif time_diff.seconds > 3600:
                time_ago = f"{time_diff.seconds // 3600}h ago"
            elif time_diff.seconds > 60:
                time_ago = f"{time_diff.seconds // 60}m ago"
            else:
                time_ago = "Just now"
            
            # Truncate content for preview
            content_preview = post['content']
            if len(content_preview) > 300:
                content_preview = content_preview[:297] + '...'
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'full_content': post['content'],
                'category': post['category'],
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'author': {
                    'name': post['author_name'],
                    'sex': post['author_sex']
                },
                'has_media': post['media_type'] != 'text'
            })
        
        return jsonify({
            'success': True,
            'data': formatted_posts,
            'page': page,
            'has_more': len(posts) == per_page
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/posts/create', methods=['POST'])
@token_required
def create_post():
    """Create a new post from mini app"""
    try:
        data = request.get_json()
        content = data.get('content', '').strip()
        category = data.get('category', 'Other')
        
        if not content:
            return jsonify({'error': 'Content cannot be empty'}), 400
        
        if len(content) > 5000:
            return jsonify({'error': 'Content too long (max 5000 characters)'}), 400
        
        # Insert post
        post_id = db_execute(
            """INSERT INTO posts (content, author_id, category, media_type) 
               VALUES (%s, %s, %s, 'text') RETURNING post_id""",
            (content, request.user_id, category),
            fetchone=True
        )
        
        if post_id:
            # Notify admin (if you want)
            return jsonify({
                'success': True,
                'message': 'Your vent has been submitted for approval',
                'post_id': post_id['post_id']
            })
        else:
            return jsonify({'error': 'Failed to create post'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/leaderboard')
@token_required
def get_leaderboard():
    """Get leaderboard data"""
    try:
        # Get top 20 users
        top_users = db_fetch_all('''
            SELECT 
                u.user_id,
                u.anonymous_name,
                u.sex,
                (SELECT COUNT(*) FROM posts WHERE author_id = u.user_id AND approved = TRUE) + 
                (SELECT COUNT(*) FROM comments WHERE author_id = u.user_id) AS total
            FROM users u
            ORDER BY total DESC
            LIMIT 20
        ''')
        
        # Get current user's rank
        user_rank = None
        user_total = calculate_user_rating(request.user_id)
        
        all_users = db_fetch_all('''
            SELECT 
                user_id,
                (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
                (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
            FROM users
            ORDER BY total DESC
        ''')
        
        for rank, user in enumerate(all_users, start=1):
            if user['user_id'] == request.user_id:
                user_rank = rank
                break
        
        # Format users
        formatted_users = []
        for idx, user in enumerate(top_users, start=1):
            formatted_users.append({
                'rank': idx,
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'points': user['total'],
                'aura': format_aura(user['total']),
                'is_current': user['user_id'] == request.user_id
            })
        
        return jsonify({
            'success': True,
            'data': {
                'top_users': formatted_users,
                'current_user': {
                    'rank': user_rank,
                    'points': user_total,
                    'aura': format_aura(user_total)
                }
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/categories')
def get_categories():
    """Get available categories"""
    categories = [
        {"code": "PrayForMe", "name": "Pray For Me", "icon": "🙏"},
        {"code": "Bible", "name": "Bible Study", "icon": "📖"},
        {"code": "WorkLife", "name": "Work and Life", "icon": "💼"},
        {"code": "SpiritualLife", "name": "Spiritual Life", "icon": "🕊️"},
        {"code": "ChristianChallenges", "name": "Christian Challenges", "icon": "⚔️"},
        {"code": "Relationship", "name": "Relationship", "icon": "❤️"},
        {"code": "Marriage", "name": "Marriage", "icon": "💍"},
        {"code": "Youth", "name": "Youth", "icon": "👥"},
        {"code": "Finance", "name": "Finance", "icon": "💰"},
        {"code": "Other", "name": "Other", "icon": "📝"}
    ]
    
    return jsonify({
        'success': True,
        'data': categories
    })

@mini_app.route('/api/profile')
@token_required
def get_profile():
    """Get user profile"""
    try:
        user = db_fetch_one('''
            SELECT user_id, anonymous_name, sex, 
                   notifications_enabled, privacy_public
            FROM users 
            WHERE user_id = %s
        ''', (request.user_id,))
        
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        rating = calculate_user_rating(request.user_id)
        
        followers = db_fetch_one(
            "SELECT COUNT(*) as count FROM followers WHERE followed_id = %s",
            (request.user_id,)
        )
        
        posts = db_fetch_one(
            "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
            (request.user_id,)
        )
        
        comments = db_fetch_one(
            "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
            (request.user_id,)
        )
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'rating': rating,
                'aura': format_aura(rating),
                'stats': {
                    'followers': followers['count'] if followers else 0,
                    'posts': posts['count'] if posts else 0,
                    'comments': comments['count'] if comments else 0
                }
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/stats')
def get_stats():
    """Get general statistics"""
    try:
        total_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = TRUE")
        total_users = db_fetch_one("SELECT COUNT(*) as count FROM users")
        total_comments = db_fetch_one("SELECT COUNT(*) as count FROM comments")
        
        return jsonify({
            'success': True,
            'data': {
                'total_posts': total_posts['count'] if total_posts else 0,
                'total_users': total_users['count'] if total_users else 0,
                'total_comments': total_comments['count'] if total_comments else 0
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health check
@mini_app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'service': 'mini-app'})

# Static files
@mini_app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# Run the mini app
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    mini_app.run(host='0.0.0.0', port=port, debug=True)
