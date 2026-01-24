# mini_app.py - Telegram Mini App Backend
import os
import json
import jwt
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template, send_from_directory
from functools import wraps
import psycopg2
from psycopg2.extras import RealDictCursor

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Initialize Flask app for mini app
mini_app = Flask(__name__, template_folder='templates', static_folder='static')
mini_app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here')

# Database connection function (reuse from your bot)
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Get database connection"""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def db_fetch_one(query, params=()):
    """Execute query and fetch one result"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchone()
        return result
    finally:
        conn.close()

def db_fetch_all(query, params=()):
    """Execute query and fetch all results"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchall()
        return result
    finally:
        conn.close()

def db_execute(query, params=()):
    """Execute query without returning results"""
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

# Helper functions from your bot (copy these)
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
        return "⚪️"

# JWT Token functions
def generate_token(user_id):
    """Generate JWT token for authentication"""
    payload = {
        'user_id': user_id,
        'exp': datetime.now(timezone.utc) + timedelta(hours=24)
    }
    return jwt.encode(payload, mini_app.secret_key, algorithm='HS256')

def verify_token(token):
    """Verify JWT token"""
    try:
        payload = jwt.decode(token, mini_app.secret_key, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

# Authentication decorator
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
        
        return f(payload['user_id'], *args, **kwargs)
    return decorated

# ==================== ROUTES ====================

@mini_app.route('/')
def index():
    """Main mini app page"""
    return render_template('mini_app.html')

@mini_app.route('/api/auth', methods=['POST'])
def authenticate():
    """Authenticate user and return token"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            return jsonify({'error': 'User ID required'}), 400
        
        # Check if user exists
        user = db_fetch_one("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Generate token
        token = generate_token(user_id)
        
        return jsonify({
            'success': True,
            'token': token,
            'expires_in': 86400  # 24 hours
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/user/profile')
@token_required
def get_user_profile(user_id):
    """Get user profile data"""
    try:
        # Get user info
        user = db_fetch_one("""
            SELECT user_id, anonymous_name, sex, 
                   notifications_enabled, privacy_public
            FROM users 
            WHERE user_id = %s
        """, (user_id,))
        
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Calculate statistics
        rating = calculate_user_rating(user_id)
        
        followers = db_fetch_one(
            "SELECT COUNT(*) as count FROM followers WHERE followed_id = %s",
            (user_id,)
        )
        followers_count = followers['count'] if followers else 0
        
        following = db_fetch_one(
            "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s",
            (user_id,)
        )
        following_count = following['count'] if following else 0
        
        posts = db_fetch_one(
            "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
            (user_id,)
        )
        posts_count = posts['count'] if posts else 0
        
        comments = db_fetch_one(
            "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
            (user_id,)
        )
        comments_count = comments['count'] if comments else 0
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'rating': rating,
                'aura': format_aura(rating),
                'stats': {
                    'followers': followers_count,
                    'following': following_count,
                    'posts': posts_count,
                    'comments': comments_count
                },
                'settings': {
                    'notifications': user['notifications_enabled'],
                    'privacy': user['privacy_public']
                }
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/posts/recent')
@token_required
def get_recent_posts(user_id):
    """Get recent posts for the feed"""
    try:
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
            LIMIT 20
        ''')
        
        formatted_posts = []
        for post in posts:
            # Calculate time ago
            post_time = post['timestamp']
            if isinstance(post_time, str):
                post_time = datetime.fromisoformat(post_time.replace('Z', '+00:00'))
            
            time_diff = datetime.utcnow() - post_time
            
            if time_diff.days > 0:
                time_ago = f"{time_diff.days}d ago"
            elif time_diff.seconds > 3600:
                time_ago = f"{time_diff.seconds // 3600}h ago"
            elif time_diff.seconds > 60:
                time_ago = f"{time_diff.seconds // 60}m ago"
            else:
                time_ago = "Just now"
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': post['content'][:200] + '...' if len(post['content']) > 200 else post['content'],
                'full_content': post['content'],
                'category': post['category'],
                'time_ago': time_ago,
                'timestamp': post['timestamp'].isoformat() if hasattr(post['timestamp'], 'isoformat') else post['timestamp'],
                'comments': post['comment_count'],
                'author': {
                    'name': post['author_name'],
                    'sex': post['author_sex']
                },
                'media_type': post['media_type']
            })
        
        return jsonify({
            'success': True,
            'data': formatted_posts
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/posts/my')
@token_required
def get_my_posts(user_id):
    """Get user's own posts"""
    try:
        posts = db_fetch_all('''
            SELECT 
                post_id, 
                content, 
                category, 
                timestamp,
                comment_count,
                approved,
                media_type
            FROM posts
            WHERE author_id = %s
            ORDER BY timestamp DESC
        ''', (user_id,))
        
        return jsonify({
            'success': True,
            'data': posts
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/comments/my')
@token_required
def get_my_comments(user_id):
    """Get user's comments"""
    try:
        comments = db_fetch_all('''
            SELECT 
                c.comment_id,
                c.content,
                c.timestamp,
                c.type,
                p.post_id,
                p.content as post_content
            FROM comments c
            JOIN posts p ON c.post_id = p.post_id
            WHERE c.author_id = %s
            ORDER BY c.timestamp DESC
            LIMIT 50
        ''', (user_id,))
        
        return jsonify({
            'success': True,
            'data': comments
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/categories')
@token_required
def get_categories(user_id):
    """Get available categories"""
    categories = [
        {"code": "PrayForMe", "name": "🙏 Pray For Me", "icon": "🙏"},
        {"code": "Bible", "name": "📖 Bible", "icon": "📖"},
        {"code": "WorkLife", "name": "💼 Work and Life", "icon": "💼"},
        {"code": "SpiritualLife", "name": "🕊 Spiritual Life", "icon": "🕊"},
        {"code": "ChristianChallenges", "name": "⚔️ Christian Challenges", "icon": "⚔️"},
        {"code": "Relationship", "name": "❤️ Relationship", "icon": "❤️"},
        {"code": "Marriage", "name": "💍 Marriage", "icon": "💍"},
        {"code": "Youth", "name": "🧑‍🤝‍🧑 Youth", "icon": "🧑‍🤝‍🧑"},
        {"code": "Finance", "name": "💰 Finance", "icon": "💰"},
        {"code": "Other", "name": "🔖 Other", "icon": "🔖"}
    ]
    
    return jsonify({
        'success': True,
        'data': categories
    })

@mini_app.route('/api/leaderboard')
@token_required
def get_leaderboard(user_id):
    """Get leaderboard data"""
    try:
        # Get top 10 users
        top_users = db_fetch_all('''
            SELECT 
                u.user_id,
                u.anonymous_name,
                u.sex,
                (SELECT COUNT(*) FROM posts WHERE author_id = u.user_id AND approved = TRUE) + 
                (SELECT COUNT(*) FROM comments WHERE author_id = u.user_id) AS total
            FROM users u
            ORDER BY total DESC
            LIMIT 10
        ''')
        
        # Get current user's rank
        user_rank = None
        user_total = calculate_user_rating(user_id)
        
        all_users = db_fetch_all('''
            SELECT 
                user_id,
                (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
                (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
            FROM users
            ORDER BY total DESC
        ''')
        
        for rank, user in enumerate(all_users, start=1):
            if user['user_id'] == user_id:
                user_rank = rank
                break
        
        formatted_users = []
        for idx, user in enumerate(top_users, start=1):
            formatted_users.append({
                'rank': idx,
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'points': user['total'],
                'aura': format_aura(user['total'])
            })
        
        return jsonify({
            'success': True,
            'data': {
                'top_users': formatted_users,
                'user_rank': user_rank,
                'user_points': user_total,
                'user_aura': format_aura(user_total)
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/messages')
@token_required
def get_messages(user_id):
    """Get user's private messages"""
    try:
        messages = db_fetch_all('''
            SELECT 
                pm.message_id,
                pm.content,
                pm.timestamp,
                pm.is_read,
                u.anonymous_name as sender_name,
                u.sex as sender_sex
            FROM private_messages pm
            JOIN users u ON pm.sender_id = u.user_id
            WHERE pm.receiver_id = %s
            ORDER BY pm.timestamp DESC
            LIMIT 50
        ''', (user_id,))
        
        # Count unread messages
        unread = db_fetch_one(
            "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE",
            (user_id,)
        )
        
        return jsonify({
            'success': True,
            'data': {
                'messages': messages,
                'unread_count': unread['count'] if unread else 0
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@mini_app.route('/api/create/post', methods=['POST'])
@token_required
def create_post(user_id):
    """Create a new post from mini app"""
    try:
        data = request.get_json()
        content = data.get('content')
        category = data.get('category')
        
        if not content or not category:
            return jsonify({'error': 'Content and category are required'}), 400
        
        # Insert post
        result = db_execute(
            "INSERT INTO posts (content, author_id, category) VALUES (%s, %s, %s)",
            (content, user_id, category)
        )
        
        if result:
            return jsonify({
                'success': True,
                'message': 'Post submitted for approval'
            })
        else:
            return jsonify({'error': 'Failed to create post'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health check endpoint
@mini_app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'service': 'mini-app'})

# Static files
@mini_app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# Run the mini app (for testing)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    mini_app.run(host='0.0.0.0', port=port, debug=True)
