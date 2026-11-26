import sqlite3
import json
import os
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

_PROJECT_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
DEFAULT_DB_PATH = os.path.expanduser(
    "~/.local/share/report-tiktok/data.db"
)

# Reason mapping table for fallback/migration
REASON_MAP = {
    "90084": "Nudity & Sexual Content",
    "90085": "Hate Speech",
    "90087": "Harassment",
    "90088": "Violence",
    "90089": "Self Harm",
    "90090": "Illegal Activities",
    "90091": "Frauds & Scams",
    "90092": "Spam"
}


class PersistenceManager:
    def __init__(self, db_path: str = None, migrate: bool = True):
        if db_path is None:
            db_path = DEFAULT_DB_PATH

        self.db_path = db_path
        # Unique session ID for the current execution instance
        self.current_session_id = f"sess_{uuid.uuid4().hex[:10]}"
        self._init_db()
        if migrate:
            self._migrate_from_old_json(db_path)

    def _init_db(self):
        db_dir = os.path.dirname(self.db_path)
        os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    metadata TEXT,
                    last_updated TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username TEXT,
                    reason_code TEXT,
                    reason_label TEXT,
                    timestamp TIMESTAMP,
                    status TEXT,
                    session_id TEXT
                )
            """)
            
            # Migration check for session_id column in existing feedback_history table
            cursor = conn.execute("PRAGMA table_info(feedback_history);")
            columns = [col[1] for col in cursor.fetchall()]
            if "session_id" not in columns:
                conn.execute("ALTER TABLE feedback_history ADD COLUMN session_id TEXT;")
            
            # Indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_username ON profiles(username);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_userid ON feedback_history(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_history(status);")
            conn.commit()

    def _migrate_from_old_json(self, db_path: str):
        """Detect existing JSON data files and migrate to SQLite if needed."""
        migration_count = {"profiles": 0, "feedback": 0}

        possible_target_paths = [
            os.path.join(_PROJECT_ROOT, "data", "targets.json"),
            os.path.join(os.path.dirname(os.path.dirname(self.db_path)), "data", "targets.json"),
        ]
        targets_found = False
        for p in possible_target_paths:
            if os.path.isfile(p):
                targets_found = True
                break

        if targets_found and not self._profile_exists_any():
            json_path = None
            for p in possible_target_paths:
                if os.path.isfile(p):
                    json_path = p
                    break
            if json_path:
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    with sqlite3.connect(self.db_path) as conn:
                        for item in data:
                            user_id = str(item.get("id", ""))
                            username = str(item.get("username", ""))
                            display_name = str(item.get("name", ""))
                            metadata = {
                                "followers": item.get("followers", 0),
                                "following": item.get("following", 0),
                                "sec_uid": item.get("sec_uid", ""),
                                "country": item.get("country", "—"),
                                "private": False,
                                "bio": "",
                            }
                            if user_id:
                                conn.execute(
                                    """INSERT OR IGNORE INTO profiles
                                       (user_id, username, display_name, metadata, last_updated)
                                       VALUES (?, ?, ?, ?, ?)""",
                                    (user_id, username, display_name,
                                     json.dumps(metadata), datetime.now().isoformat())
                                )
                        conn.commit()
                        migration_count["profiles"] = len(data)
                except Exception as e:
                    print(f"[warn] Migration from targets.json failed: {e}")

        # Check feedback history migration
        possible_req_paths = [
            os.path.join(_PROJECT_ROOT, "data", "important_requests.json"),
            os.path.join(os.path.dirname(os.path.dirname(self.db_path)), "data", "important_requests.json"),
        ]
        requests_found = False
        for p in possible_req_paths:
            if os.path.isfile(p):
                requests_found = True
                break

        if requests_found and not self._feedback_exists_any():
            json_path = None
            for p in possible_req_paths:
                if os.path.isfile(p):
                    json_path = p
                    break
            if json_path:
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    with sqlite3.connect(self.db_path) as conn:
                        for item in data:
                            user_id = str(item.get("target_id", ""))
                            username = str(item.get("target", ""))
                            reason_code = str(item.get("reason", ""))
                            
                            # Preserve vector or map reason code if vector/category is missing or generic
                            reason_label = item.get("vector") or item.get("category")
                            if not reason_label or reason_label == "—":
                                reason_label = REASON_MAP.get(reason_code, "Unknown / Not Recorded")
                            
                            status = str(item.get("type", "UNKNOWN"))
                            timestamp = item.get("time")
                            if timestamp and len(timestamp) == 8: # e.g. "00:14:03"
                                # Combine with today or keep as is
                                timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {timestamp}"
                            else:
                                timestamp = datetime.now().isoformat()

                            conn.execute(
                                """INSERT INTO feedback_history
                                   (user_id, username, reason_code, reason_label, timestamp, status, session_id)
                                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                                (user_id, username, reason_code, reason_label, timestamp, status)
                            )
                        conn.commit()
                        migration_count["feedback"] = len(data)
                except Exception as e:
                    print(f"[warn] Migration from important_requests.json failed: {e}")

        if migration_count["profiles"] > 0 or migration_count["feedback"] > 0:
            print(
                f"[info] Migration complete: {migration_count['profiles']} profiles, "
                f"{migration_count['feedback']} feedback records imported to SQLite at {db_path}"
            )

    def _profile_exists_any(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM profiles")
            return cursor.fetchone()[0] > 0

    def _feedback_exists_any(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM feedback_history")
            return cursor.fetchone()[0] > 0

    def save_profile(self, user_id: str, username: str, display_name: str,
                     metadata: Dict[str, Any]):
        """Save or update a user profile in the cache."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO profiles
                   (user_id, username, display_name, metadata, last_updated)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(user_id), str(username), str(display_name),
                 json.dumps(metadata), datetime.now().isoformat())
            )
            conn.commit()

    def get_profile(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Retrieve a profile by user_id or username."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT user_id, username, display_name, metadata, last_updated "
                "FROM profiles WHERE user_id = ? OR username = ?",
                (str(identifier), str(identifier))
            )
            row = cursor.fetchone()
            if row:
                return {
                    "user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "metadata": json.loads(row[3]) if row[3] else {},
                    "last_updated": row[4]
                }
            return None

    def list_all_profiles(self) -> List[Dict[str, Any]]:
        """Return all cached profiles."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT user_id, username, display_name, metadata, last_updated FROM profiles"
            )
            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append({
                    "user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "metadata": json.loads(row[3]) if row[3] else {},
                    "last_updated": row[4]
                })
            return result

    def log_feedback(self, user_id: str, username: str,
                     reason_code: str, reason_label: str, status: str):
        """Record a feedback submission in the history associated with current session."""
        if not reason_label or reason_label == "—":
            reason_label = REASON_MAP.get(str(reason_code), "Unknown / Not Recorded")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO feedback_history
                   (user_id, username, reason_code, reason_label, timestamp, status, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(user_id), str(username), str(reason_code),
                 str(reason_label), datetime.now().isoformat(), str(status), self.current_session_id)
            )
            conn.commit()

    def get_account_history_summaries(self) -> List[Dict[str, Any]]:
        """Group feedback records by TikTok User ID for account-centric history view."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT 
                    f.user_id,
                    COALESCE(p.username, f.username, 'Unknown') as username,
                    COUNT(f.id) as total_records,
                    SUM(CASE WHEN f.status LIKE '%ACCEPTED%' OR f.status = 'success' THEN 1 ELSE 0 END) as accepted_count,
                    SUM(CASE WHEN f.status LIKE '%REJECTED%' OR f.status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN f.status = 'PENDING' THEN 1 ELSE 0 END) as pending_count,
                    SUM(CASE WHEN f.status NOT LIKE '%ACCEPTED%' AND f.status NOT LIKE '%REJECTED%' AND f.status != 'success' AND f.status != 'failed' AND f.status != 'PENDING' THEN 1 ELSE 0 END) as unknown_count,
                    MIN(f.timestamp) as first_timestamp,
                    MAX(f.timestamp) as last_timestamp,
                    COUNT(DISTINCT f.session_id) as session_count
                FROM feedback_history f
                LEFT JOIN profiles p ON f.user_id = p.user_id
                WHERE f.user_id IS NOT NULL AND f.user_id != ''
                GROUP BY f.user_id
                ORDER BY last_timestamp DESC
            """)
            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append({
                    "user_id": row[0],
                    "username": row[1],
                    "total_records": row[2],
                    "accepted_count": row[3],
                    "failed_count": row[4],
                    "pending_count": row[5],
                    "unknown_count": row[6],
                    "first_timestamp": row[7],
                    "last_timestamp": row[8],
                    "session_count": row[9] if row[9] > 0 else "Unknown"
                })
            return result

    def get_account_details(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Get detailed records and summary for a specific account by user_id or username."""
        profile = self.get_profile(identifier)
        user_id = profile.get("user_id") if profile else None

        with sqlite3.connect(self.db_path) as conn:
            if not user_id:
                # Try finding by user_id or username in feedback history
                cursor = conn.execute(
                    "SELECT user_id FROM feedback_history WHERE user_id = ? OR username = ? LIMIT 1",
                    (identifier, identifier)
                )
                row = cursor.fetchone()
                if row:
                    user_id = row[0]

            if not user_id:
                return None

            if not profile:
                profile = {"user_id": user_id, "username": identifier, "display_name": identifier, "metadata": {}}

            cursor = conn.execute("""
                SELECT id, reason_code, reason_label, timestamp, status, session_id
                FROM feedback_history
                WHERE user_id = ?
                ORDER BY timestamp DESC
            """, (user_id,))
            records = []
            for r in cursor.fetchall():
                records.append({
                    "id": r[0],
                    "reason_code": r[1],
                    "reason_label": r[2] or "Unknown / Not Recorded",
                    "timestamp": r[3],
                    "status": r[4],
                    "session_id": r[5]
                })

            return {
                "profile": profile,
                "records": records
            }

    def get_feedback_history(self, user_id: str) -> List[Dict[str, Any]]:
        """Get feedback history records for a specific user.

        Returns a list of dictionaries containing feedback records with
        reason_code and status keys. Used for duplicate check purposes.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT reason_code, reason_label, timestamp, status, session_id
                FROM feedback_history
                WHERE user_id = ?
                ORDER BY timestamp DESC
            """, (user_id,))
            records = []
            for r in cursor.fetchall():
                records.append({
                    "reason_code": r[0],
                    "reason_label": r[1] or "Unknown / Not Recorded",
                    "timestamp": r[2],
                    "status": r[3],
                    "session_id": r[4]
                })
            return records

    def register_account_profile(self, user_id: str, username: str, display_name: str = "") -> bool:
        """Register or update an account profile explicitly without triggering reports."""
        user_id = str(user_id).strip()
        username = str(username).strip().lstrip("@")
        if not user_id:
            return False

        existing = self.get_profile(user_id)
        metadata = existing.get("metadata", {}) if existing else {}
        if not display_name and existing:
            display_name = existing.get("display_name", username)
        elif not display_name:
            display_name = username

        self.save_profile(user_id, username, display_name, metadata)
        return True
