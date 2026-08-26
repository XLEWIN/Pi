"""Database module — SQLite operations for user storage and logging."""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Database file path
DB_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DB_DIR / "bot_database.db"


class Database:
    """SQLite database manager for the bot."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.connection: Optional[sqlite3.Connection] = None
        self.connect()
        self.create_tables()

    def connect(self):
        """Connect to the SQLite database."""
        try:
            self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            logger.info(f"Connected to database: {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database connection error: {e}")
            raise

    def close(self):
        """Close the database connection."""
        if self.connection:
            self.connection.close()
            logger.info("Database connection closed")

    def create_tables(self):
        """Create necessary tables if they don't exist."""
        cursor = self.connection.cursor()

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_bot INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_messages INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                is_muted INTEGER DEFAULT 0
            )
        """)

        # User activity log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                chat_id INTEGER,
                chat_title TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Moderation actions log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS moderation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moderator_id INTEGER,
                target_id INTEGER,
                action TEXT,
                reason TEXT,
                chat_id INTEGER,
                chat_title TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration TEXT
            )
        """)

        # Group info
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                chat_title TEXT,
                member_count INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Group members
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER,
                user_id INTEGER,
                role TEXT DEFAULT 'member',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id),
                FOREIGN KEY (chat_id) REFERENCES groups(chat_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # ── Filters table ─────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                trigger_word TEXT NOT NULL,
                reply_text TEXT,
                buttons_json TEXT,
                media_type TEXT,
                media_id TEXT,
                UNIQUE(chat_id, trigger_word)
            )
        """)

        # ── Blocklist table ───────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                action TEXT DEFAULT 'delete',
                reason TEXT DEFAULT 'Blocked word',
                UNIQUE(chat_id, word)
            )
        """)

        # ── Blocklist exemptions ──────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocklist_exemptions (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        # ── Sudo users ────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sudo_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Gbanned users ─────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gbanned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT 'No reason provided',
                banned_by INTEGER,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Watch words ───────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watch_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                mode TEXT DEFAULT 'copy',
                UNIQUE(chat_id, admin_id, word)
            )
        """)

        # ── Welcome/Goodbye settings ──────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS welcome_settings (
                chat_id INTEGER PRIMARY KEY,
                welcome_enabled INTEGER DEFAULT 1,
                goodbye_enabled INTEGER DEFAULT 1,
                clean_welcome INTEGER DEFAULT 0,
                clean_goodbye INTEGER DEFAULT 0,
                clean_service INTEGER DEFAULT 0,
                last_welcome_msg_id INTEGER,
                last_goodbye_msg_id INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS welcome_messages (
                chat_id INTEGER PRIMARY KEY,
                welcome_text TEXT DEFAULT 'Hey {first}, welcome to {chatname}! 👋',
                welcome_buttons TEXT,
                welcome_media TEXT,
                welcome_media_type TEXT,
                goodbye_text TEXT DEFAULT 'Sad to see you leaving {first}. Take Care! 👋',
                goodbye_buttons TEXT,
                goodbye_media TEXT,
                goodbye_media_type TEXT
            )
        """)

        # ── Leveling system ───────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_level (
                user_id INTEGER PRIMARY KEY,
                global_level INTEGER DEFAULT 1,
                global_xp INTEGER DEFAULT 0,
                global_messages INTEGER DEFAULT 0,
                template INTEGER DEFAULT 1,
                streak_current INTEGER DEFAULT 0,
                streak_best INTEGER DEFAULT 0,
                last_message_date TEXT,
                last_streak_date TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_chat_level (
                chat_id INTEGER,
                user_id INTEGER,
                messages INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                xp INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_messages (
                chat_id INTEGER,
                user_id INTEGER,
                date TEXT,
                messages INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, date)
            )
        """)

        self.connection.commit()
        logger.info("Database tables created/verified")

    def add_user(self, user_id: int, username: str = None, first_name: str = None,
                 last_name: str = None, is_bot: bool = False) -> bool:
        """Add or update a user in the database."""
        try:
            cursor = self.connection.cursor()
            now = datetime.now().isoformat()

            # Check if user exists
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            exists = cursor.fetchone()

            if exists:
                # Update existing user
                cursor.execute("""
                    UPDATE users 
                    SET username = COALESCE(?, username),
                        first_name = COALESCE(?, first_name),
                        last_name = COALESCE(?, last_name),
                        last_seen = ?
                    WHERE user_id = ?
                """, (username, first_name, last_name, now, user_id))
            else:
                # Insert new user
                cursor.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, is_bot, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, username, first_name, last_name, 1 if is_bot else 0, now, now))

            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding user {user_id}: {e}")
            return False

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user data by ID."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user data by username."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Error getting user by username {username}: {e}")
            return None

    def update_user_activity(self, user_id: int, action: str, chat_id: int = None,
                             chat_title: str = None, details: str = None):
        """Log user activity."""
        try:
            cursor = self.connection.cursor()
            now = datetime.now().isoformat()

            # Update last_seen
            cursor.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (now, user_id))

            # Insert activity log
            cursor.execute("""
                INSERT INTO user_activity (user_id, action, chat_id, chat_title, details)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, action, chat_id, chat_title, details))

            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error updating user activity: {e}")

    def add_group(self, chat_id: int, chat_title: str) -> bool:
        """Add or update a group."""
        try:
            cursor = self.connection.cursor()
            now = datetime.now().isoformat()

            cursor.execute("SELECT chat_id FROM groups WHERE chat_id = ?", (chat_id,))
            exists = cursor.fetchone()

            if exists:
                cursor.execute("""
                    UPDATE groups 
                    SET chat_title = ?, last_active = ?
                    WHERE chat_id = ?
                """, (chat_title, now, chat_id))
            else:
                cursor.execute("""
                    INSERT INTO groups (chat_id, chat_title, first_seen, last_active)
                    VALUES (?, ?, ?, ?)
                """, (chat_id, chat_title, now, now))

            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding group {chat_id}: {e}")
            return False

    def add_group_member(self, chat_id: int, user_id: int, role: str = "member"):
        """Add a user to a group's member list."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO group_members (chat_id, user_id, role)
                VALUES (?, ?, ?)
            """, (chat_id, user_id, role))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error adding group member: {e}")

    def log_moderation(self, moderator_id: int, target_id: int, action: str,
                       reason: str, chat_id: int, chat_title: str, duration: str = None):
        """Log a moderation action."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT INTO moderation_log 
                (moderator_id, target_id, action, reason, chat_id, chat_title, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (moderator_id, target_id, action, reason, chat_id, chat_title, duration))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error logging moderation action: {e}")

    def get_user_count(self) -> int:
        """Get total number of registered users."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_bot = 0")
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error getting user count: {e}")
            return 0

    def get_group_count(self) -> int:
        """Get total number of groups."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM groups")
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error getting group count: {e}")
            return 0

    def get_recent_activity(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent user activity."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT ua.*, u.username, u.first_name 
                FROM user_activity ua
                LEFT JOIN users u ON ua.user_id = u.user_id
                ORDER BY ua.timestamp DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting recent activity: {e}")
            return []

    def get_moderation_log(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent moderation actions."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT * FROM moderation_log
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting moderation log: {e}")
            return []

    # ── Filters ───────────────────────────────────────────
    def add_filter(self, chat_id: int, trigger: str, text: str = None,
                   buttons_json: str = None, media_type: str = None, media_id: str = None) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO filters (chat_id, trigger_word, reply_text, buttons_json, media_type, media_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, trigger.lower(), text, buttons_json, media_type, media_id))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding filter: {e}")
            return False

    def get_filters(self, chat_id: int) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM filters WHERE chat_id = ?", (chat_id,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting filters: {e}")
            return []

    def get_filter(self, chat_id: int, trigger: str) -> Optional[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM filters WHERE chat_id = ? AND trigger_word = ?", (chat_id, trigger.lower()))
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting filter: {e}")
            return None

    def remove_filter(self, chat_id: int, trigger: str) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM filters WHERE chat_id = ? AND trigger_word = ?", (chat_id, trigger.lower()))
            self.connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing filter: {e}")
            return False

    # ── Blocklist ─────────────────────────────────────────
    def add_blocklist_word(self, chat_id: int, word: str, action: str = "delete", reason: str = "Blocked word") -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO blocklist (chat_id, word, action, reason)
                VALUES (?, ?, ?, ?)
            """, (chat_id, word.lower(), action, reason))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding blocklist word: {e}")
            return False

    def get_blocklist(self, chat_id: int) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM blocklist WHERE chat_id = ?", (chat_id,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting blocklist: {e}")
            return []

    def remove_blocklist_word(self, chat_id: int, word: str) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM blocklist WHERE chat_id = ? AND word = ?", (chat_id, word.lower()))
            self.connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing blocklist word: {e}")
            return False

    def clear_blocklist(self, chat_id: int) -> int:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM blocklist WHERE chat_id = ?", (chat_id,))
            self.connection.commit()
            return cursor.rowcount
        except sqlite3.Error as e:
            logger.error(f"Error clearing blocklist: {e}")
            return 0

    def set_blocklist_action(self, chat_id: int, action: str):
        try:
            cursor = self.connection.cursor()
            cursor.execute("UPDATE blocklist SET action = ? WHERE chat_id = ?", (action, chat_id))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting blocklist action: {e}")

    def set_blocklist_reason(self, chat_id: int, reason: str):
        try:
            cursor = self.connection.cursor()
            cursor.execute("UPDATE blocklist SET reason = ? WHERE chat_id = ?", (reason, chat_id))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting blocklist reason: {e}")

    def exempt_blocklist_user(self, chat_id: int, user_id: int):
        try:
            cursor = self.connection.cursor()
            cursor.execute("INSERT OR IGNORE INTO blocklist_exemptions (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error exempting user: {e}")

    def is_blocklist_exempt(self, chat_id: int, user_id: int) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1 FROM blocklist_exemptions WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            return False

    # ── Sudo users ────────────────────────────────────────
    def add_sudo_user(self, user_id: int, added_by: int = None) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("INSERT OR REPLACE INTO sudo_users (user_id, added_by) VALUES (?, ?)", (user_id, added_by))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding sudo user: {e}")
            return False

    def remove_sudo_user(self, user_id: int) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM sudo_users WHERE user_id = ?", (user_id,))
            self.connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing sudo user: {e}")
            return False

    def get_sudo_users(self) -> List[int]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT user_id FROM sudo_users")
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting sudo users: {e}")
            return []

    def is_sudo_user(self, user_id: int) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1 FROM sudo_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            return False

    # ── Gbanned users ─────────────────────────────────────
    def add_gban(self, user_id: int, reason: str = "No reason provided", banned_by: int = None) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("INSERT OR REPLACE INTO gbanned_users (user_id, reason, banned_by) VALUES (?, ?, ?)",
                           (user_id, reason, banned_by))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding gban: {e}")
            return False

    def remove_gban(self, user_id: int) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM gbanned_users WHERE user_id = ?", (user_id,))
            self.connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing gban: {e}")
            return False

    def get_gbanned_users(self) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM gbanned_users")
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting gbanned users: {e}")
            return []

    def is_gbanned(self, user_id: int) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1 FROM gbanned_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            return False

    # ── Watch words ───────────────────────────────────────
    def add_watch_word(self, chat_id: int, admin_id: int, word: str, mode: str = "copy") -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO watch_words (chat_id, admin_id, word, mode)
                VALUES (?, ?, ?, ?)
            """, (chat_id, admin_id, word.lower(), mode))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding watch word: {e}")
            return False

    def remove_watch_word(self, chat_id: int, admin_id: int, word: str) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM watch_words WHERE chat_id = ? AND admin_id = ? AND word = ?",
                           (chat_id, admin_id, word.lower()))
            self.connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing watch word: {e}")
            return False

    def get_watch_words(self, chat_id: int, admin_id: int) -> List[str]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT word FROM watch_words WHERE chat_id = ? AND admin_id = ?", (chat_id, admin_id))
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting watch words: {e}")
            return []

    def get_all_watch_words(self, chat_id: int) -> Dict[int, List[str]]:
        """Get all watch words for a chat, grouped by admin_id."""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT admin_id, word FROM watch_words WHERE chat_id = ?", (chat_id,))
            result = {}
            for row in cursor.fetchall():
                admin_id = row[0]
                word = row[1]
                if admin_id not in result:
                    result[admin_id] = []
                result[admin_id].append(word)
            return result
        except sqlite3.Error as e:
            logger.error(f"Error getting all watch words: {e}")
            return {}

    def get_watch_mode(self, chat_id: int, admin_id: int) -> str:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT mode FROM watch_words WHERE chat_id = ? AND admin_id = ? LIMIT 1", (chat_id, admin_id))
            row = cursor.fetchone()
            return row[0] if row else "copy"
        except sqlite3.Error as e:
            return "copy"

    def set_watch_mode(self, chat_id: int, admin_id: int, mode: str):
        try:
            cursor = self.connection.cursor()
            cursor.execute("UPDATE watch_words SET mode = ? WHERE chat_id = ? AND admin_id = ?", (mode, chat_id, admin_id))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting watch mode: {e}")

    # ── Welcome/Goodbye ──────────────────────────────────
    def get_welcome_settings(self, chat_id: int) -> Dict[str, Any]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM welcome_settings WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {"chat_id": chat_id, "welcome_enabled": 1, "goodbye_enabled": 1,
                    "clean_welcome": 0, "clean_goodbye": 0, "clean_service": 0,
                    "last_welcome_msg_id": None, "last_goodbye_msg_id": None}
        except sqlite3.Error as e:
            logger.error(f"Error getting welcome settings: {e}")
            return {}

    def set_welcome_enabled(self, chat_id: int, enabled: bool):
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, 1 if enabled else 0,
                  self.get_welcome_settings(chat_id).get("goodbye_enabled", 1),
                  self.get_welcome_settings(chat_id).get("clean_welcome", 0),
                  self.get_welcome_settings(chat_id).get("clean_goodbye", 0),
                  self.get_welcome_settings(chat_id).get("clean_service", 0)))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting welcome enabled: {e}")

    def set_goodbye_enabled(self, chat_id: int, enabled: bool):
        try:
            cursor = self.connection.cursor()
            settings = self.get_welcome_settings(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, settings.get("welcome_enabled", 1), 1 if enabled else 0,
                  settings.get("clean_welcome", 0), settings.get("clean_goodbye", 0), settings.get("clean_service", 0)))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting goodbye enabled: {e}")

    def set_clean_welcome(self, chat_id: int, enabled: bool):
        try:
            cursor = self.connection.cursor()
            settings = self.get_welcome_settings(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, settings.get("welcome_enabled", 1), settings.get("goodbye_enabled", 1),
                  1 if enabled else 0, settings.get("clean_goodbye", 0), settings.get("clean_service", 0)))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting clean welcome: {e}")

    def set_clean_goodbye(self, chat_id: int, enabled: bool):
        try:
            cursor = self.connection.cursor()
            settings = self.get_welcome_settings(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, settings.get("welcome_enabled", 1), settings.get("goodbye_enabled", 1),
                  settings.get("clean_welcome", 0), 1 if enabled else 0, settings.get("clean_service", 0)))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting clean goodbye: {e}")

    def update_last_welcome_msg(self, chat_id: int, msg_id: int):
        try:
            cursor = self.connection.cursor()
            settings = self.get_welcome_settings(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service, last_welcome_msg_id, last_goodbye_msg_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, settings.get("welcome_enabled", 1), settings.get("goodbye_enabled", 1),
                  settings.get("clean_welcome", 0), settings.get("clean_goodbye", 0), settings.get("clean_service", 0),
                  msg_id, settings.get("last_goodbye_msg_id")))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error updating last welcome msg: {e}")

    def update_last_goodbye_msg(self, chat_id: int, msg_id: int):
        try:
            cursor = self.connection.cursor()
            settings = self.get_welcome_settings(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_settings (chat_id, welcome_enabled, goodbye_enabled, clean_welcome, clean_goodbye, clean_service, last_welcome_msg_id, last_goodbye_msg_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, settings.get("welcome_enabled", 1), settings.get("goodbye_enabled", 1),
                  settings.get("clean_welcome", 0), settings.get("clean_goodbye", 0), settings.get("clean_service", 0),
                  settings.get("last_welcome_msg_id"), msg_id))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error updating last goodbye msg: {e}")

    def get_welcome_message(self, chat_id: int) -> Dict[str, Any]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM welcome_messages WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {"chat_id": chat_id,
                    "welcome_text": "Hey {first}, welcome to {chatname}! 👋",
                    "welcome_buttons": None, "welcome_media": None, "welcome_media_type": None,
                    "goodbye_text": "Sad to see you leaving {first}. Take Care! 👋",
                    "goodbye_buttons": None, "goodbye_media": None, "goodbye_media_type": None}
        except sqlite3.Error as e:
            logger.error(f"Error getting welcome message: {e}")
            return {}

    def set_welcome_text(self, chat_id: int, text: str):
        try:
            cursor = self.connection.cursor()
            msg = self.get_welcome_message(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_messages (chat_id, welcome_text, welcome_buttons, welcome_media, welcome_media_type, goodbye_text, goodbye_buttons, goodbye_media, goodbye_media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, text, msg.get("welcome_buttons"), msg.get("welcome_media"), msg.get("welcome_media_type"),
                  msg.get("goodbye_text"), msg.get("goodbye_buttons"), msg.get("goodbye_media"), msg.get("goodbye_media_type")))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting welcome text: {e}")

    def set_goodbye_text(self, chat_id: int, text: str):
        try:
            cursor = self.connection.cursor()
            msg = self.get_welcome_message(chat_id)
            cursor.execute("""
                INSERT OR REPLACE INTO welcome_messages (chat_id, welcome_text, welcome_buttons, welcome_media, welcome_media_type, goodbye_text, goodbye_buttons, goodbye_media, goodbye_media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, msg.get("welcome_text"), msg.get("welcome_buttons"), msg.get("welcome_media"), msg.get("welcome_media_type"),
                  text, msg.get("goodbye_buttons"), msg.get("goodbye_media"), msg.get("goodbye_media_type")))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting goodbye text: {e}")

    def reset_welcome(self, chat_id: int):
        self.set_welcome_text(chat_id, "Hey {first}, welcome to {chatname}! 👋")

    def reset_goodbye(self, chat_id: int):
        self.set_goodbye_text(chat_id, "Sad to see you leaving {first}. Take Care! 👋")

    # ── Leveling system ──────────────────────────────────
    def get_user_level(self, user_id: int) -> Dict[str, Any]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM user_level WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {"user_id": user_id, "global_level": 1, "global_xp": 0, "global_messages": 0,
                    "template": 1, "streak_current": 0, "streak_best": 0,
                    "last_message_date": None, "last_streak_date": None}
        except sqlite3.Error as e:
            logger.error(f"Error getting user level: {e}")
            return {}

    def update_user_level(self, user_id: int, **kwargs):
        try:
            cursor = self.connection.cursor()
            existing = self.get_user_level(user_id)
            cursor.execute("""
                INSERT OR REPLACE INTO user_level (user_id, global_level, global_xp, global_messages, template, streak_current, streak_best, last_message_date, last_streak_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                kwargs.get("global_level", existing.get("global_level", 1)),
                kwargs.get("global_xp", existing.get("global_xp", 0)),
                kwargs.get("global_messages", existing.get("global_messages", 0)),
                kwargs.get("template", existing.get("template", 1)),
                kwargs.get("streak_current", existing.get("streak_current", 0)),
                kwargs.get("streak_best", existing.get("streak_best", 0)),
                kwargs.get("last_message_date", existing.get("last_message_date")),
                kwargs.get("last_streak_date", existing.get("last_streak_date")),
            ))
            self.connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error updating user level: {e}")

    def add_message_xp(self, user_id: int, chat_id: int) -> Tuple[int, int, bool]:
        """Add XP for a message. Returns (level_ups, new_level, leveled_up)."""
        from datetime import date
        today = date.today().isoformat()

        # Update global messages
        user = self.get_user_level(user_id)
        global_msgs = user.get("global_messages", 0) + 1
        global_level = user.get("global_level", 1)
        global_xp = user.get("global_xp", 0)
        leveled_up = False

        # +1 level every 100 messages globally
        new_global_level = (global_msgs // 100) + 1
        if new_global_level > global_level:
            leveled_up = True
            global_level = new_global_level

        # Add XP per message (10-20 XP)
        import random
        xp_gain = random.randint(10, 20)
        global_xp += xp_gain

        # Update streak
        streak_current = user.get("streak_current", 0)
        streak_best = user.get("streak_best", 0)
        last_msg_date = user.get("last_message_date")
        last_streak_date = user.get("last_streak_date")

        if last_msg_date != today:
            from datetime import date, timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            if last_msg_date == yesterday:
                streak_current += 1
            else:
                streak_current = 1
            if streak_current > streak_best:
                streak_best = streak_current

        self.update_user_level(user_id,
            global_level=global_level,
            global_xp=global_xp,
            global_messages=global_msgs,
            streak_current=streak_current,
            streak_best=streak_best,
            last_message_date=today,
        )

        # Update chat messages and level
        cursor = self.connection.cursor()
        cursor.execute("SELECT messages, level FROM user_chat_level WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        row = cursor.fetchone()
        if row:
            chat_msgs = row[0] + 1
            chat_level = row[1]
        else:
            chat_msgs = 1
            chat_level = 1

        # +1 level every 50 messages per chat
        new_chat_level = (chat_msgs // 50) + 1
        chat_level_up = new_chat_level > chat_level

        cursor.execute("""
            INSERT OR REPLACE INTO user_chat_level (chat_id, user_id, messages, level, xp)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, user_id, chat_msgs, new_chat_level, chat_msgs * 10))
        self.connection.commit()

        # Update daily messages
        cursor.execute("SELECT messages FROM daily_messages WHERE chat_id = ? AND user_id = ? AND date = ?", (chat_id, user_id, today))
        daily_row = cursor.fetchone()
        daily_msgs = (daily_row[0] if daily_row else 0) + 1
        cursor.execute("""
            INSERT OR REPLACE INTO daily_messages (chat_id, user_id, date, messages)
            VALUES (?, ?, ?, ?)
        """, (chat_id, user_id, today, daily_msgs))
        self.connection.commit()

        return (1 if leveled_up else 0) + (1 if chat_level_up else 0), global_level, leveled_up

    def get_chat_level(self, chat_id: int, user_id: int) -> Dict[str, Any]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT * FROM user_chat_level WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {"chat_id": chat_id, "user_id": user_id, "messages": 0, "level": 1, "xp": 0}
        except sqlite3.Error as e:
            return {}

    def get_leaderboard(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT ucl.*, u.username, u.first_name
                FROM user_chat_level ucl
                LEFT JOIN users u ON ucl.user_id = u.user_id
                WHERE ucl.chat_id = ?
                ORDER BY ucl.level DESC, ucl.xp DESC
                LIMIT ?
            """, (chat_id, limit))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []

    def get_global_leaderboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT ul.*, u.username, u.first_name
                FROM user_level ul
                LEFT JOIN users u ON ul.user_id = u.user_id
                ORDER BY ul.global_level DESC, ul.global_xp DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []

    def get_daily_top(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        from datetime import date
        today = date.today().isoformat()
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT dm.*, u.username, u.first_name
                FROM daily_messages dm
                LEFT JOIN users u ON dm.user_id = u.user_id
                WHERE dm.chat_id = ? AND dm.date = ?
                ORDER BY dm.messages DESC
                LIMIT ?
            """, (chat_id, today, limit))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []

    def get_period_top(self, chat_id: int, days: int, limit: int = 10) -> List[Dict[str, Any]]:
        from datetime import date, timedelta
        start_date = (date.today() - timedelta(days=days)).isoformat()
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT dm.user_id, SUM(dm.messages) as total_messages, u.username, u.first_name
                FROM daily_messages dm
                LEFT JOIN users u ON dm.user_id = u.user_id
                WHERE dm.chat_id = ? AND dm.date >= ?
                GROUP BY dm.user_id
                ORDER BY total_messages DESC
                LIMIT ?
            """, (chat_id, start_date, limit))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []

    def set_template(self, user_id: int, template: int):
        self.update_user_level(user_id, template=template)

    def get_user_rank_in_chat(self, chat_id: int, user_id: int) -> int:
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT COUNT(*) + 1 as rank
                FROM user_chat_level
                WHERE chat_id = ? AND (level > ? OR (level = ? AND xp > ?))
            """, (chat_id,
                  *self._get_chat_level_tuple(chat_id, user_id)))
            row = cursor.fetchone()
            return row[0] if row else 1
        except sqlite3.Error as e:
            return 1

    def _get_chat_level_tuple(self, chat_id: int, user_id: int):
        data = self.get_chat_level(chat_id, user_id)
        return (data.get("level", 1), data.get("level", 1), data.get("xp", 0))

    def get_total_chat_members(self, chat_id: int) -> int:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_chat_level WHERE chat_id = ?", (chat_id,))
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            return 0


# Global database instance
db = Database()
