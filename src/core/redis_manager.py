# -*- coding: utf-8 -*-
"""
Redis Session & State Manager for THUNDER TikTok Reporter.
Handles session storage, multi-session pooling, rotation, and health verification.
"""

import os
import json
import time
import random
from typing import Optional, Dict, List, Any

try:
    import redis
except ImportError:
    redis = None


def get_redis_client() -> Optional[Any]:
    """Initialize Redis client using REDIS_URL environment variable or local .env file."""
    if redis is None:
        return None

    # Load from .env if present
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
    redis_url = os.getenv('REDIS_URL')
    if not redis_url and os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('REDIS_URL='):
                        redis_url = line.split('=', 1)[1].strip()
                        break
        except Exception:
            pass

    if not redis_url:
        redis_url = 'redis://localhost:6379/0'

    try:
        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        if client.ping():
            return client
    except Exception:
        pass
    return None


class RedisSessionManager:
    SESSION_PREFIX = "tiktok:session:"
    ACTIVE_SET = "tiktok:sessions:active"

    LOCAL_JSON_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'saved_sessions.json')

    def __init__(self, client: Optional[Any] = None):
        self.client = client or get_redis_client()

    @property
    def is_connected(self) -> bool:
        if not self.client:
            return False
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def parse_cookie_string(self, raw_str: str) -> Dict[str, str]:
        """Convert cookie header string or key/value pairs into dictionary."""
        cookies = {}
        if not raw_str:
            return cookies
        if ';' in raw_str or '=' in raw_str:
            for piece in raw_str.split(';'):
                if '=' in piece:
                    k, v = piece.strip().split('=', 1)
                    cookies[k.strip()] = v.strip()
        else:
            cookies['sessionid'] = raw_str.strip()
            cookies['sessionid_ss'] = raw_str.strip()
            cookies['sid_tt'] = raw_str.strip()
        return cookies

    def _load_local_sessions(self) -> List[Dict[str, Any]]:
        """Load sessions from local data/saved_sessions.json file."""
        if os.path.exists(self.LOCAL_JSON_FILE):
            try:
                with open(self.LOCAL_JSON_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def _save_local_sessions(self, sessions: List[Dict[str, Any]]):
        """Save sessions to local data/saved_sessions.json file."""
        try:
            os.makedirs(os.path.dirname(self.LOCAL_JSON_FILE), exist_ok=True)
            with open(self.LOCAL_JSON_FILE, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def save_session(self, identifier: str, cookies_input: Any, user_label: str = "", **extra_fields) -> bool:
        """Store a session in Redis and local JSON file with full metadata support."""
        if isinstance(cookies_input, str):
            cookie_dict = self.parse_cookie_string(cookies_input)
        elif isinstance(cookies_input, dict):
            cookie_dict = cookies_input
        else:
            return False

        if not cookie_dict:
            return False

        session_data = {
            "identifier": identifier,
            "label": user_label or identifier,
            "cookies": json.dumps(cookie_dict),
            "cookies_dict": cookie_dict,
            "sessionid": cookie_dict.get('sessionid', ''),
            "status": "active",
            "added_at": int(time.time()),
            "last_used": int(time.time()),
        }
        # Merge extra metadata (user_id, sec_user_id, username, screen_name, verified_user, etc.)
        for k, v in extra_fields.items():
            if v is not None:
                session_data[k] = v

        # 1. Always save to local storage
        local_sessions = self._load_local_sessions()
        local_sessions = [s for s in local_sessions if s.get('identifier') != identifier]
        local_sessions.append(session_data)
        self._save_local_sessions(local_sessions)

        # 2. Save to Redis if connected
        if self.is_connected:
            key = f"{self.SESSION_PREFIX}{identifier}"
            try:
                r_data = dict(session_data)
                r_data.pop('cookies_dict', None)
                r_data_clean = {k: str(v) for k, v in r_data.items()}
                self.client.hset(key, mapping=r_data_clean)
                self.client.sadd(self.ACTIVE_SET, identifier)
            except Exception:
                pass

        return True

    def get_session(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific session by its identifier."""
        if self.is_connected:
            key = f"{self.SESSION_PREFIX}{identifier}"
            try:
                data = self.client.hgetall(key)
                if data:
                    if 'cookies' in data:
                        data['cookies_dict'] = json.loads(data['cookies'])
                    return data
            except Exception:
                pass

        for s in self._load_local_sessions():
            if s.get('identifier') == identifier:
                if 'cookies_dict' not in s and 'cookies' in s:
                    try:
                        s['cookies_dict'] = json.loads(s['cookies'])
                    except Exception:
                        pass
                return s
        return None

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        """List active sessions from Redis and/or local saved_sessions.json."""
        sessions_map: Dict[str, Dict[str, Any]] = {}

        # Local JSON sessions
        for s in self._load_local_sessions():
            ident = s.get('identifier')
            if ident and s.get('status', 'active') == 'active':
                if 'cookies_dict' not in s and 'cookies' in s:
                    try:
                        s['cookies_dict'] = json.loads(s['cookies'])
                    except Exception:
                        pass
                sessions_map[ident] = s

        # Merge with Redis sessions. Fetch missing hashes through a single
        # pipeline instead of one PING+HGETALL per session, which kept the UI
        # blocked for minutes when the pool was large.
        if self.is_connected:
            try:
                active_ids = self.client.smembers(self.ACTIVE_SET)
                missing = [sid for sid in active_ids if sid not in sessions_map]
                if missing:
                    pipe = self.client.pipeline(transaction=False)
                    for sid in missing:
                        pipe.hgetall(f"{self.SESSION_PREFIX}{sid}")
                    for sid, data in zip(missing, pipe.execute()):
                        if not data:
                            continue
                        data = dict(data)
                        if 'cookies' in data:
                            try:
                                data['cookies_dict'] = json.loads(data['cookies'])
                            except Exception:
                                pass
                        if data.get('status', 'active') == 'active':
                            sessions_map[sid] = data
            except Exception:
                pass

        session_list = list(sessions_map.values())
        for s in session_list:
            src = s.get('source_file', '')
            if 'followers' not in s or 'likes' not in s:
                import re
                fm = re.search(r'\[(\d+)\s+followers\]', src)
                lm = re.search(r'\[(\d+)\s+likes\]', src)
                s['followers'] = int(fm.group(1)) if fm else 0
                s['likes'] = int(lm.group(1)) if lm else 0
            else:
                try:
                    s['followers'] = int(s.get('followers', 0))
                    s['likes'] = int(s.get('likes', 0))
                except Exception:
                    s['followers'] = 0
                    s['likes'] = 0

        # Sort accounts with most followers and likes first
        session_list.sort(key=lambda x: (x.get('followers', 0), x.get('likes', 0)), reverse=True)
        return session_list

    def get_random_session(self) -> Optional[Dict[str, Any]]:
        """Pick a random active session from the pool."""
        sessions = self.get_active_sessions()
        if not sessions:
            return None
        chosen = random.choice(sessions)
        sid = chosen.get('identifier')
        if sid and self.is_connected:
            try:
                self.client.hset(f"{self.SESSION_PREFIX}{sid}", "last_used", int(time.time()))
            except Exception:
                pass
        return chosen

    def mark_expired(self, identifier: str):
        """Mark a session as expired in Redis and local storage."""
        if self.is_connected:
            try:
                self.client.srem(self.ACTIVE_SET, identifier)
                self.client.hset(f"{self.SESSION_PREFIX}{identifier}", "status", "expired")
            except Exception:
                pass
        local_sessions = self._load_local_sessions()
        for s in local_sessions:
            if s.get('identifier') == identifier:
                s['status'] = 'expired'
        self._save_local_sessions(local_sessions)

    def delete_session(self, identifier: str) -> bool:
        """Completely delete session from Redis and local storage."""
        if self.is_connected:
            try:
                self.client.srem(self.ACTIVE_SET, identifier)
                self.client.delete(f"{self.SESSION_PREFIX}{identifier}")
            except Exception:
                pass
        local_sessions = [s for s in self._load_local_sessions() if s.get('identifier') != identifier]
        self._save_local_sessions(local_sessions)
        return True
