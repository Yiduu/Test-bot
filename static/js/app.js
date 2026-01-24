// Christian Vent Mini App - Frontend JavaScript

class ChristianVentApp {
    constructor() {
        this.token = null;
        this.user = null;
        this.currentTab = 'feed';
        this.telegramWebApp = null;
        
        this.init();
    }

    async init() {
        // Check if running in Telegram WebApp
        if (window.Telegram && window.Telegram.WebApp) {
            this.telegramWebApp = Telegram.WebApp;
            this.telegramWebApp.ready();
            this.telegramWebApp.expand();
            
            // Get user from Telegram
            const user = this.telegramWebApp.initDataUnsafe.user;
            if (user) {
                await this.authenticateWithTelegram(user.id);
            }
        }
        
        // Check for token in URL
        const urlParams = new URLSearchParams(window.location.search);
        const urlToken = urlParams.get('token');
        
        if (urlToken) {
            this.token = urlToken;
            localStorage.setItem('cv_token', urlToken);
            await this.loadUserProfile();
        } else {
            // Check localStorage
            const storedToken = localStorage.getItem('cv_token');
            if (storedToken) {
                this.token = storedToken;
                await this.loadUserProfile();
            }
        }
        
        this.setupEventListeners();
        await this.loadInitialData();
    }

    async authenticateWithTelegram(userId) {
        try {
            const response = await fetch('/api/auth', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ user_id: userId })
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.token = data.token;
                this.user = data.user;
                localStorage.setItem('cv_token', this.token);
                this.updateUserSection();
            }
        } catch (error) {
            console.error('Authentication failed:', error);
        }
    }

    async loadUserProfile() {
        if (!this.token) return;
        
        try {
            const response = await fetch('/api/profile', {
                headers: {
                    'Authorization': `Bearer ${this.token}`
                }
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.user = data.data;
                this.updateUserSection();
            } else {
                // Token expired or invalid
                localStorage.removeItem('cv_token');
                this.token = null;
            }
        } catch (error) {
            console.error('Failed to load profile:', error);
        }
    }

    updateUserSection() {
        const userSection = document.getElementById('userSection');
        if (!userSection || !this.user) return;
        
        userSection.innerHTML = `
            <div class="user-avatar">
                ${this.user.sex || '👤'}
            </div>
            <div class="user-info">
                <h3>${this.user.name}</h3>
                <p>${this.user.aura} Level ${Math.floor(this.user.rating / 10) + 1}</p>
            </div>
        `;
    }

    setupEventListeners() {
        // Tab navigation
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                this.switchTab(e.target.dataset.tab);
            });
        });

        // Refresh buttons
        document.getElementById('refreshFeed')?.addEventListener('click', () => this.loadFeed());
        document.getElementById('refreshLeaderboard')?.addEventListener('click', () => this.loadLeaderboard());

        // Vent submission
        const ventText = document.getElementById('ventText');
        const charCount = document.getElementById('charCount');
        const submitBtn = document.getElementById('submitVent');

        if (ventText && charCount) {
            ventText.addEventListener('input', () => {
                charCount.textContent = `${ventText.value.length}/5000`;
            });
        }

        if (submitBtn) {
            submitBtn.addEventListener('click', () => this.submitVent());
        }

        // Modal close
        document.querySelector('.close-modal')?.addEventListener('click', () => {
            document.getElementById('postModal').style.display = 'none';
        });

        // Close modal when clicking outside
        window.addEventListener('click', (e) => {
            const modal = document.getElementById('postModal');
            if (e.target === modal) {
                modal.style.display = 'none';
            }
        });
    }

    async loadInitialData() {
        await this.loadFeed();
        await this.loadStats();
    }

    async loadFeed(page = 1) {
        const feedContainer = document.getElementById('feedContainer');
        if (!feedContainer) return;

        feedContainer.innerHTML = '<div class="loading-spinner">Loading vents...</div>';

        try {
            const response = await fetch(`/api/feed?page=${page}`, {
                headers: this.token ? {
                    'Authorization': `Bearer ${this.token}`
                } : {}
            });

            const data = await response.json();

            if (data.success) {
                this.renderFeed(data.data);
            } else {
                feedContainer.innerHTML = `
                    <div class="error-message">
                        Failed to load feed. Please try again.
                    </div>
                `;
            }
        } catch (error) {
            console.error('Failed to load feed:', error);
            feedContainer.innerHTML = `
                <div class="error-message">
                    Network error. Please check your connection.
                </div>
            `;
        }
    }

    renderFeed(posts) {
        const feedContainer = document.getElementById('feedContainer');
        if (!feedContainer) return;

        if (posts.length === 0) {
            feedContainer.innerHTML = `
                <div class="empty-state">
                    <h3>No vents yet</h3>
                    <p>Be the first to share what's on your heart</p>
                </div>
            `;
            return;
        }

        feedContainer.innerHTML = posts.map(post => `
            <div class="post-card" onclick="app.showPostDetail(${post.id})">
                <div class="post-header">
                    <div class="author-icon">
                        ${post.author.sex || '👤'}
                    </div>
                    <div class="author-info">
                        <h4>${post.author.name}</h4>
                        <div class="post-meta">
                            <span class="post-category">${post.category}</span>
                            <span>•</span>
                            <span>${post.time_ago}</span>
                        </div>
                    </div>
                </div>
                
                <div class="post-content ${post.content.length > 300 ? 'expandable' : ''}">
                    ${this.escapeHtml(post.content)}
                </div>
                
                <div class="post-footer">
                    <div class="comment-count">
                        💬 ${post.comments} comment${post.comments !== 1 ? 's' : ''}
                    </div>
                    ${post.content.length > 300 ? `
                        <button class="read-more" onclick="event.stopPropagation(); app.expandPost(this)">
                            Read more
                        </button>
                    ` : ''}
                </div>
            </div>
        `).join('');
    }

    async loadLeaderboard() {
        const container = document.getElementById('leaderboardContainer');
        if (!container) return;

        container.innerHTML = '<div class="loading-spinner">Loading leaderboard...</div>';

        try {
            const response = await fetch('/api/leaderboard', {
                headers: this.token ? {
                    'Authorization': `Bearer ${this.token}`
                } : {}
            });

            const data = await response.json();

            if (data.success) {
                this.renderLeaderboard(data.data);
            }
        } catch (error) {
            console.error('Failed to load leaderboard:', error);
        }
    }

    renderLeaderboard(data) {
        const container = document.getElementById('leaderboardContainer');
        if (!container) return;

        const { top_users, current_user } = data;

        container.innerHTML = `
            <div class="leaderboard-header">
                <div class="current-user-stats">
                    <h3>Your Rank: #${current_user.rank}</h3>
                    <p>${current_user.points} points ${current_user.aura}</p>
                </div>
            </div>
            
            ${top_users.map((user, index) => `
                <div class="leaderboard-item ${user.is_current ? 'current-user-highlight' : ''}">
                    <div class="leaderboard-rank rank-${index + 1}">
                        ${index + 1}
                    </div>
                    <div class="leaderboard-user">
                        <div class="user-avatar-small">
                            ${user.sex || '👤'}
                        </div>
                        <div class="user-info-small">
                            <h4>${user.name}</h4>
                            <p>${user.aura} Contributor</p>
                        </div>
                    </div>
                    <div class="leaderboard-points">
                        ${user.points} pts
                    </div>
                </div>
            `).join('')}
        `;
    }

    async submitVent() {
        const ventText = document.getElementById('ventText');
        const categorySelect = document.getElementById('categorySelect');
        const submitBtn = document.getElementById('submitVent');

        if (!ventText || !categorySelect || !submitBtn) return;

        const content = ventText.value.trim();
        const category = categorySelect.value;

        if (!content) {
            this.showMessage('Please write something before posting', 'error');
            return;
        }

        if (!this.token) {
            this.showMessage('Please log in first', 'error');
            return;
        }

        // Disable button and show loading
        const originalText = submitBtn.textContent;
        submitBtn.textContent = 'Posting...';
        submitBtn.disabled = true;

        try {
            const response = await fetch('/api/posts/create', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${this.token}`
                },
                body: JSON.stringify({
                    content,
                    category
                })
            });

            const data = await response.json();

            if (data.success) {
                this.showMessage('Your vent has been submitted for approval', 'success');
                ventText.value = '';
                document.getElementById('charCount').textContent = '0/5000';
                
                // Switch to feed tab
                this.switchTab('feed');
                await this.loadFeed();
            } else {
                this.showMessage(data.error || 'Failed to submit vent', 'error');
            }
        } catch (error) {
            console.error('Failed to submit vent:', error);
            this.showMessage('Network error. Please try again.', 'error');
        } finally {
            submitBtn.textContent = originalText;
            submitBtn.disabled = false;
        }
    }

    async loadStats() {
        const statsBar = document.getElementById('statsBar');
        if (!statsBar) return;

        try {
            const response = await fetch('/api/stats');
            const data = await response.json();

            if (data.success) {
                statsBar.innerHTML = `
                    <span>${data.data.total_posts} Vents</span>
                    <span>•</span>
                    <span>${data.data.total_users} Users</span>
                    <span>•</span>
                    <span>${data.data.total_comments} Comments</span>
                `;
            }
        } catch (error) {
            console.error('Failed to load stats:', error);
        }
    }

    async showPostDetail(postId) {
        // For now, show a simple modal
        // In the future, you could fetch full post details
        const modal = document.getElementById('postModal');
        const modalContent = document.getElementById('modalContent');

        modalContent.innerHTML = `
            <div class="loading-spinner">Loading post...</div>
        `;
        modal.style.display = 'block';

        // Note: You'll need to add an API endpoint to get single post details
        // For now, just show a message
        setTimeout(() => {
            modalContent.innerHTML = `
                <div class="post-detail">
                    <h3>Post Details</h3>
                    <p>Full post view coming soon. For now, you can view this post in the Telegram bot.</p>
                    <div class="modal-actions">
                        <button class="submit-btn" onclick="window.open('https://t.me/${BOT_USERNAME}?start=comments_${postId}', '_blank')">
                            View in Bot
                        </button>
                    </div>
                </div>
            `;
        }, 500);
    }

    expandPost(button) {
        const postCard = button.closest('.post-card');
        const content = postCard.querySelector('.post-content');
        
        if (content.classList.contains('expanded')) {
            content.classList.remove('expanded');
            button.textContent = 'Read more';
        } else {
            content.classList.add('expanded');
            button.textContent = 'Show less';
        }
    }

    switchTab(tabName) {
        // Update tab buttons
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });

        // Update tab panes
        document.querySelectorAll('.tab-pane').forEach(pane => {
            pane.classList.toggle('active', pane.id === `${tabName}-tab`);
        });

        this.currentTab = tabName;

        // Load data for the tab if needed
        switch (tabName) {
            case 'feed':
                this.loadFeed();
                break;
            case 'leaderboard':
                this.loadLeaderboard();
                break;
            case 'profile':
                this.loadProfile();
                break;
        }
    }

    async loadProfile() {
        const container = document.getElementById('profileContainer');
        if (!container) return;

        if (!this.user) {
            container.innerHTML = `
                <div class="error-message">
                    Please log in to view your profile
                </div>
            `;
            return;
        }

        container.innerHTML = `
            <div class="profile-header">
                <div class="profile-avatar">
                    ${this.user.sex || '👤'}
                </div>
                <h2>${this.user.name}</h2>
                <div class="profile-rating">
                    ${this.user.aura} ${this.user.rating} points
                </div>
            </div>
            
            <div class="profile-stats">
                <div class="stat-card">
                    <div class="stat-number">${this.user.stats?.posts || 0}</div>
                    <div class="stat-label">Vents</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">${this.user.stats?.comments || 0}</div>
                    <div class="stat-label">Comments</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">${this.user.stats?.followers || 0}</div>
                    <div class="stat-label">Followers</div>
                </div>
            </div>
            
            <div class="profile-actions">
                <button class="submit-btn" onclick="app.switchTab('vent')">
                    Share a Vent
                </button>
            </div>
        `;
    }

    showMessage(message, type = 'info') {
        // Create message element
        const messageEl = document.createElement('div');
        messageEl.className = `${type}-message`;
        messageEl.textContent = message;

        // Add to top of main content
        const mainContent = document.querySelector('.main-content');
        mainContent.insertBefore(messageEl, mainContent.firstChild);

        // Remove after 5 seconds
        setTimeout(() => {
            messageEl.remove();
        }, 5000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Global app instance
let app;

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    app = new ChristianVentApp();
});

// Make app available globally for inline handlers
window.app = app;
