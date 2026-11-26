# -*- coding: utf-8 -*-
"""
THUNDER – Web Dashboard Backend
Flask + Flask-SocketIO server that wraps the existing report logic
with a REST API and real-time WebSocket updates.

Run:  python dashboard.py
Open:  http://localhost:5000
"""

import sys
import os

# Ensure local modules and core package are importable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
SRC_DIR = os.path.join(REPO_ROOT, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import time
import json
import random
import re
import threading
from collections import deque
from binascii import hexlify
from random import randbytes
from uuid import uuid4
from datetime import datetime
from urllib.parse import urlencode

import pycountry
from flask import Flask, render_template, request as flask_request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO

from core import (
    UserAgent, Gorgon, Ladon, Argus, ProxyManager, make_request_with_proxy, tls_get,
    RedisSessionManager, TelegramReporterBot,
    pick_web_device, generate_web_device_id, get_web_device_profiles
)
from core.proxy_manager import load_proxy_file
from core.project_logger import ProjectLogger
redis_mgr = RedisSessionManager()

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder=os.path.join(REPO_ROOT, 'templates'))
app.config['SECRET_KEY'] = os.environ.get('DASHBOARD_SECRET', 'dev-secret-key-change-me')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ── Report reasons ────────────────────────────────────────────────────────────
REPORT_REASONS = {
    '1': ('90084', 'porn',               'Nudity & Sexual Content / Pornography'),
    '2': ('90085', 'hate_speech',        'Hate Speech & Harassment'),
    '3': ('90087', 'harassment',         'Bullying & Harassment'),
    '4': ('90088', 'violence',           'Violence, Abuse & Dangerous Content'),
    '5': ('90089', 'self_harm',          'Suicide, Self-harm & Eating Disorders'),
    '6': ('90090', 'illegal_activities', 'Illegal Activities & Regulated Goods'),
    '7': ('90091', 'frauds_and_scams',   'Frauds, Scams & Deceptive Content'),
    '8': ('90092', 'spam',               'Spam, Fake Engagement & Misleading Info'),
}


def parse_report_reasons(body) -> list:
    """Resolve a request payload into ordered (key, code, category, label) entries."""
    raw = body.get('reasons')
    if raw is None or raw == [] or raw == '':
        raw = [body.get('reason', '1')]
    if isinstance(raw, (str, int, float)):
        raw = str(raw).split(',')

    entries = []
    seen = set()
    for item in raw if isinstance(raw, (list, tuple)) else [raw]:
        key = str(item).strip()
        if not key:
            continue
        for reason_key, (code, category, label) in REPORT_REASONS.items():
            if key in (reason_key, code):
                if code not in seen:
                    entries.append((reason_key, code, category, label))
                    seen.add(code)
                break

    if not entries:
        reason_key = '1'
        code, category, label = REPORT_REASONS[reason_key]
        entries.append((reason_key, code, category, label))
    return entries


# ── Persistence files ─────────────────────────────────────────────────────────
DATA_DIR = os.path.join(REPO_ROOT, 'data')
LOGS_DIR = os.path.join(REPO_ROOT, 'logs')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
REGISTERED_DEVICES_FILE = os.path.join(DATA_DIR, 'registered_devices.json')
IMPORTANT_REQUESTS_FILE = os.path.join(DATA_DIR, 'important_requests.json')
LAST_TARGET_FILE = os.path.join(DATA_DIR, 'last_target.txt')


def save_last_target(username: str) -> None:
    """Persist the most recently targeted username for the next run."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_TARGET_FILE, 'w', encoding='utf-8') as f:
            f.write((username or '').lstrip('@'))
    except OSError:
        pass

# Current attack log file (set fresh on each /api/start)
current_log_file: str = ''
log_file_lock = threading.Lock()
SESSIONS_FILE = os.path.join(DATA_DIR, 'saved_sessions.json')

# ── Global dashboard state ────────────────────────────────────────────────────
BLOCK_DURATION = 600  # 10 min cooldown per device model after captcha

state_lock = threading.RLock()
stop_event = threading.Event()

state = {
    'running': False,
    'target': {},
    'config': {
        'mode':        'mobile',
        'reason_code': '90084',
        'category':    'porn',
        'reason_label': 'Nudity & Sexual Content / Pornography',
        'reason_keys': ['1'],
        'reasons': [
            {'key': '1', 'code': '90084', 'category': 'porn',
             'label': 'Nudity & Sexual Content / Pornography'},
        ],
        'session_id':  '',
        'web_cookies': {},
        'delay':       1.0,
        'use_proxy':   False,
        'proxy_file':  'proxies.txt',
        'web_device':  'random',
    },
    'stats': {
        'accepted':     0,
        'rejected':     0,
        'captcha_hits': 0,
        'start_time':   None,
    },
    'last_request':    {},
    'request_log':     deque(maxlen=50),
    'proxy_stats':     None,
    'blocked_devices': {},
    'workers':         [],
    'reason_index':    0,
}

proxy_manager = None  # set when user starts with proxies
project_logger = None  # detailed per-project request/response logger

def dashboard_stats_provider() -> dict:
    with state_lock:
        st = state['stats']
        total = st['accepted'] + st['rejected']
        cfg = state['config']
        tgt = state['target']
        cooldowns = sum(1 for exp in state.get('blocked_devices', {}).values() if time.time() < exp)
        proxy_info = proxy_manager.stats() if proxy_manager else ('Enabled' if cfg.get('use_proxy') else 'Direct')
        return {
            'total': total,
            'accepted': st['accepted'],
            'rejected': st['rejected'],
            'rate_pct': (st['accepted'] / total * 100) if total else 0.0,
            'captcha_hits': st['captcha_hits'],
            'active_cooldowns': cooldowns,
            'target': tgt.get('username', 'target'),
            'target_id': tgt.get('id', '—'),
            'mode': cfg.get('mode', 'mobile'),
            'delay': cfg.get('delay', 1.0),
            'reason': cfg.get('reason_code', '—'),
            'web_device': cfg.get('web_device', 'random'),
            'proxy_info': proxy_info,
            'last_req': dict(state.get('last_request') or {}),
        }

def dashboard_target_provider() -> dict:
    with state_lock:
        return dict(state.get('target') or {})

def dashboard_stop_action():
    stop_event.set()
    with state_lock:
        state['running'] = False
    socketio.emit('status_change', {'running': False})

telegram_bot = TelegramReporterBot.from_env(
    stats_provider=dashboard_stats_provider,
    target_info_provider=dashboard_target_provider,
    stop_action=dashboard_stop_action,
)

# ── Session & Persistence helpers ─────────────────────────────────────────────

def validate_tiktok_session(cookies_dict, proxy=None):
    """
    Directly query TikTok's account verification endpoint to check if cookies / sessionid are active.
    Returns: (is_valid: bool, username_or_error: str)
    """
    if not cookies_dict:
        return False, "No cookies provided"

    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        'Referer': 'https://www.tiktok.com/',
        'Accept': 'application/json, text/plain, */*',
    }
    url = 'https://www.tiktok.com/passport/web/account/info/?aid=1988'
    try:
        res = tls_get(url, headers=headers, cookies=cookies_dict, timeout=7)
        if res.status_code != 200:
            return False, f"TikTok responded with HTTP {res.status_code}"
        data = res.json()
        if data.get('message') == 'success':
            u_info = data.get('data') or {}
            uname = u_info.get('username') or u_info.get('user_id') or 'active_user'
            return True, uname
        else:
            err = (data.get('data') or {}).get('description') or data.get('message') or 'Session is expired or invalid'
            return False, err
    except Exception as e:
        return False, f"Validation request failed: {e}"


def get_all_stored_sessions():
    """Retrieve sessions from Redis if connected, otherwise fallback to local JSON file."""
    sessions = []
    if redis_mgr.is_connected:
        sessions = redis_mgr.get_active_sessions()
    
    if not sessions and os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
                sessions = json.load(f)
        except Exception:
            sessions = []
    return sessions


def save_stored_session(identifier, cookies_dict, label="", verified_user=""):
    """Save session to Redis if available, and also persist to local JSON file."""
    saved_redis = False
    if redis_mgr.is_connected:
        saved_redis = redis_mgr.save_session(identifier, cookies_dict, label)
        if saved_redis and verified_user:
            try:
                redis_mgr.client.hset(f"{redis_mgr.SESSION_PREFIX}{identifier}", "verified_user", verified_user)
            except Exception:
                pass

    # Persist locally in data/saved_sessions.json
    try:
        sessions = []
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
                sessions = json.load(f)
        # Update or add
        existing = [s for s in sessions if s.get('identifier') != identifier and s.get('label') != identifier]
        existing.append({
            'identifier': identifier,
            'label': label or identifier,
            'cookies': json.dumps(cookies_dict),
            'cookies_dict': cookies_dict,
            'sessionid': cookies_dict.get('sessionid', ''),
            'status': 'active',
            'verified_user': verified_user,
            'added_at': int(time.time()),
            'last_verified': int(time.time()),
        })
        with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return saved_redis


def update_stored_session_status(identifier, status, verified_user=""):
    """Update active/expired status and verified username in Redis and local JSON file."""
    if redis_mgr.is_connected:
        if status == 'expired':
            redis_mgr.mark_expired(identifier)
        else:
            try:
                key = f"{redis_mgr.SESSION_PREFIX}{identifier}"
                redis_mgr.client.hset(key, mapping={"status": status, "verified_user": verified_user, "last_verified": int(time.time())})
                redis_mgr.client.sadd(redis_mgr.ACTIVE_SET, identifier)
            except Exception:
                pass

    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
                sessions = json.load(f)
            changed = False
            for s in sessions:
                if s.get('identifier') == identifier or s.get('label') == identifier:
                    s['status'] = status
                    if verified_user:
                        s['verified_user'] = verified_user
                    s['last_verified'] = int(time.time())
                    changed = True
            if changed:
                with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(sessions, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def delete_stored_session(identifier):
    """Completely delete session from Redis and local JSON file."""
    deleted_redis = False
    if redis_mgr.is_connected:
        deleted_redis = redis_mgr.delete_session(identifier)

    deleted_file = False
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
                sessions = json.load(f)
            new_sessions = [s for s in sessions if s.get('identifier') != identifier and s.get('label') != identifier]
            if len(new_sessions) != len(sessions):
                with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(new_sessions, f, indent=2, ensure_ascii=False)
                deleted_file = True
        except Exception:
            pass
    return deleted_redis or deleted_file


def pick_any_active_session():
    """Pick any active session from Redis or local JSON file."""
    if redis_mgr.is_connected:
        s = redis_mgr.get_random_session()
        if s:
            return s
    stored = get_all_stored_sessions()
    for s in stored:
        if s.get('status') == 'active':
            if 'cookies_dict' in s:
                return s
            if 'cookies' in s and isinstance(s['cookies'], str):
                try:
                    s['cookies_dict'] = json.loads(s['cookies'])
                    return s
                except Exception:
                    pass
    return None


def save_registered_device(entry: dict):
    with state_lock:
        try:
            data = []
            if os.path.exists(REGISTERED_DEVICES_FILE):
                with open(REGISTERED_DEVICES_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            if not any(d.get('device_id') == entry.get('device_id') for d in data):
                data.append(entry)
                with open(REGISTERED_DEVICES_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def save_important_request(entry: dict):
    with state_lock:
        try:
            data = []
            if os.path.exists(IMPORTANT_REQUESTS_FILE):
                with open(IMPORTANT_REQUESTS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data.append(entry)
            if len(data) > 500:
                data = data[-500:]
            with open(IMPORTANT_REQUESTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def log_attack_request(entry: dict):
    """Save full details of each attack attempt to the active attack log file."""
    global current_log_file
    if not current_log_file:
        return
    with log_file_lock:
        try:
            with open(current_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception:
            pass


# ── Scoring ───────────────────────────────────────────────────────────────────

def calculate_request_score(success, http_status, server_code, latency_ms):
    if success and server_code == '0':
        base = 10.0
    elif success:
        base = 8.5
    elif http_status == '200':
        base = 6.0
    elif http_status in ('403', '429') or server_code == '1000':
        base = 2.5
    elif http_status == '—':
        base = 1.0
    else:
        base = 3.0
    if latency_ms > 2500:
        base -= 2.0
    elif latency_ms > 1200:
        base -= 1.0
    elif latency_ms > 600:
        base -= 0.5
    return max(1.0, min(10.0, round(base, 1)))


# ── Device management ─────────────────────────────────────────────────────────

def pick_available_device():
    now = time.time()
    for _ in range(20):
        cand = UserAgent()
        model_name = f"{cand['brand']} {cand['type']}"
        with state_lock:
            blocked_until = state['blocked_devices'].get(model_name, 0)
        if now >= blocked_until:
            return cand
    return UserAgent()


# ── TikTok info lookup ────────────────────────────────────────────────────────

def get_tiktok_info(username, proxy_mgr=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;'
                  'q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'sec-fetch-site': 'none',
        'sec-fetch-mode': 'navigate',
        'upgrade-insecure-requests': '1',
    }
    url = f'https://www.tiktok.com/@{username}'
    proxy_dict = proxy_mgr.get() if proxy_mgr else None
    proxies = {k: v for k, v in proxy_dict.items()
               if k != '_entry'} if proxy_dict else None
    # Try fetching profile HTML (with proxy if healthy, then direct fallback)
    res = None
    last_err = None
    attempts = [proxies, None] if proxies else [None]
    
    for p_try in attempts:
        try:
            res = tls_get(url, headers=headers, proxies=p_try, timeout=15)
            if res.status_code == 200 and ('webapp.user-detail' in res.text or '__UNIVERSAL_DATA_FOR_REHYDRATION__' in res.text):
                break
        except Exception as e:
            last_err = e
            continue

    if res is None or res.status_code != 200 or ('webapp.user-detail' not in res.text and '__UNIVERSAL_DATA_FOR_REHYDRATION__' not in res.text):
        # Fallback to InfoTik from ms4 if web scraping failed or timed out
        try:
            from ms4 import InfoTik
            ms_info = InfoTik.TikTok_Info(username)
            if ms_info and ms_info.get('status') == 'ok' and ms_info.get('id'):
                dt = ms_info.get('Date')
                date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt or '—')
                return {
                    'id':        str(ms_info.get('id', '')),
                    'name':      ms_info.get('name', '') or username,
                    'sec_uid':   ms_info.get('secuid', ''),
                    'username':  username,
                    'followers': ms_info.get('followers', 0),
                    'following': ms_info.get('following', 0),
                    'likes':     ms_info.get('like', 0),
                    'country':   ms_info.get('country', '') or '—',
                    'private':   str(ms_info.get('private', 'false')).lower(),
                    'bio':       ms_info.get('bio', ''),
                    'date':      date_str,
                    'status':    'ok',
                }
        except Exception:
            pass

    if res is None:
        return {'status': 'bad', 'error': str(last_err or 'Connection timed out')}

    # Strategy 1 – __UNIVERSAL_DATA_FOR_REHYDRATION__
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        res.text,
    )
    if match:
        try:
            data = json.loads(match.group(1))
            ds = data.get('__DEFAULT_SCOPE__', {})
            ud = ds.get('webapp.user-detail', {})
            if ud.get('statusCode') == 10221:
                return {
                    'status': 'bad',
                    'statusCode': 10221,
                    'error': 'Account not found, banned, or handle changed (TikTok code 10221)'
                }
            ui = ud.get('userInfo', {}).get('user', {})
            st = ud.get('userInfo', {}).get('stats', {})
            if ui and ui.get('id'):
                uid = str(ui['id'])
                reg_date = '—'
                if uid.isdigit():
                    b = '{0:b}'.format(int(uid))
                    if len(b) >= 32:
                        try:
                            reg_date = datetime.fromtimestamp(
                                int(b[:31], 2)
                            ).strftime('%Y-%m-%d')
                        except Exception:
                            pass
                region = ui.get('region', '')
                c_name, c_flag = region, ''
                if region:
                    try:
                        c_obj = pycountry.countries.get(alpha_2=region)
                        if c_obj:
                            c_name = c_obj.name
                            c_flag = c_obj.flag
                    except Exception:
                        pass
                return {
                    'id':        uid,
                    'name':      ui.get('nickname', ''),
                    'sec_uid':   ui.get('secUid', ''),
                    'username':  username,
                    'followers': st.get('followerCount', 0),
                    'following': st.get('followingCount', 0),
                    'likes':     st.get('heartCount', 0),
                    'country':   f"{c_name} {c_flag}".strip() or '—',
                    'private':   str(ui.get('privateAccount', False)),
                    'bio':       ui.get('signature', ''),
                    'date':      reg_date,
                    'status':    'ok',
                }
        except Exception:
            pass

    # Strategy 2 – inline data scrape
    if 'webapp.user-detail"' in res.text:
        try:
            g = str(res.text.split('webapp.user-detail"')[1]).split(
                '"RecommendUserList"'
            )[0]

            def ex(k, end='",'):
                try:
                    return str(g.split(k)[1]).split(end)[0]
                except Exception:
                    return ''

            uid = ex('id":"')
            nickname = ex('nickname":"')
            bio = ex('signature":"')
            region = ex('region":"')
            followers = ex('followerCount":', ',"')
            following = ex('followingCount":', ',"')
            like = ex('heart":', ',"')
            private = ex('privateAccount":', ',"')
            reg_date = '—'
            if uid.isdigit():
                b = '{0:b}'.format(int(uid))
                if len(b) >= 32:
                    try:
                        reg_date = datetime.fromtimestamp(
                            int(b[:31], 2)
                        ).strftime('%Y-%m-%d')
                    except Exception:
                        pass
            c_name, c_flag = region, ''
            if region:
                try:
                    c_obj = pycountry.countries.get(alpha_2=region)
                    if c_obj:
                        c_name = c_obj.name
                        c_flag = c_obj.flag
                except Exception:
                    pass
            sec_uid = ex('secUid":"')
            return {
                'id':        uid,
                'name':      nickname,
                'sec_uid':   sec_uid,
                'username':  username,
                'followers': followers,
                'following': following,
                'likes':     like,
                'country':   f"{c_name} {c_flag}".strip() or '—',
                'private':   private,
                'bio':       bio,
                'date':      reg_date,
                'status':    'ok',
            }
        except Exception:
            pass

    return {'status': 'bad', 'error': 'Account not found or restricted'}


def find_cached_target(username: str) -> dict:
    """Search targets.json, important_requests.json, and attack logs for historical target metadata."""
    if not username:
        return {}
    clean_user = username.strip().lstrip('@')
    targets_file = os.path.join(DATA_DIR, 'targets.json')
    req_file = os.path.join(DATA_DIR, 'important_requests.json')

    # 1. Check targets.json
    try:
        if os.path.exists(targets_file):
            with open(targets_file, 'r', encoding='utf-8') as f:
                for entry in json.load(f):
                    if entry.get('username') == clean_user and entry.get('id') and str(entry.get('id')) not in ('—', '', '0', 'None'):
                        return entry
    except Exception:
        pass

    # 2. Check important_requests.json
    try:
        if os.path.exists(req_file):
            with open(req_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for item in reversed(data):
                    if item.get('target') == clean_user and item.get('target_id') and str(item.get('target_id')) not in ('—', '', '0', 'None'):
                        return {'id': str(item['target_id']), 'username': clean_user, 'name': clean_user}
    except Exception:
        pass

    # 3. Check logs/attack_{username}_*.jsonl
    try:
        if os.path.exists(LOGS_DIR):
            for fname in sorted(os.listdir(LOGS_DIR), reverse=True):
                if fname.startswith(f"attack_{clean_user}_") and fname.endswith(".jsonl"):
                    with open(os.path.join(LOGS_DIR, fname), 'r', encoding='utf-8') as f:
                        for line in f:
                            try:
                                d = json.loads(line.strip())
                                tid = d.get('target_id')
                                if tid and str(tid) not in ('—', '', '0', 'None'):
                                    return {'id': str(tid), 'username': clean_user, 'name': clean_user}
                            except Exception:
                                pass
    except Exception:
        pass

    return {}


# ── Core report function ─────────────────────────────────────────────────────

def do_report():
    global proxy_manager

    with state_lock:
        target_info = state['target']
        target_id = target_info.get('id', '')
        target_name = target_info.get('name', '')
        target_user = target_info.get('username', '')
        sec_uid = target_info.get('sec_uid', '')
        mode = state['config'].get('mode', 'mobile')
        reason_code = state['config'].get('reason_code', '90084')
        category = state['config'].get('category', 'porn')
        reason_label = state['config'].get('reason_label', 'Nudity & Sexual Content / Pornography')
        configured_reasons = state['config'].get('reasons') or []
        if configured_reasons:
            idx = int(state.get('reason_index', 0)) % len(configured_reasons)
            state['reason_index'] = idx + 1
            chosen_reason = configured_reasons[idx]
            reason_code = str(chosen_reason.get('code') or reason_code)
            category = chosen_reason.get('category') or category
            reason_label = chosen_reason.get('label') or reason_label
        session_id = state['config'].get('session_id', '')
        web_cookies = state['config'].get('web_cookies', {})

    # Auto-pick/rotate from Redis session pool if set to rotate or empty
    if not web_cookies or session_id in ('rotate', 'pool', ''):
        active_s = pick_any_active_session()
        if active_s:
            web_cookies = active_s.get('cookies_dict', {})
            session_id = active_s.get('sessionid', '')

    if mode == 'web':
        url = 'https://www.tiktok.com/aweme/v2/aweme/feedback/'
        web_dev_pref = state['config'].get('web_device', 'random')
        web_dev = pick_web_device(web_dev_pref)
        brand = web_dev['brand']
        dev_type = web_dev['type']
        device_full_name = web_dev['full_name']
        agent = web_dev['user_agent']
        _now = datetime.now().strftime('%H:%M:%S')
        _did = web_cookies.get('device_id') or generate_web_device_id()
        _iid = 'web'
        _udid = 'web'
        _cdid = 'web'

        save_registered_device({
            'registered_at': _now,
            'brand':         brand,
            'model':         dev_type,
            'device_id':     _did,
            'install_id':    _iid,
            'openudid':      _udid,
            'cdid':          _cdid,
        })

        params = {
            'aid': '1988',
            'app_language': 'en',
            'app_name': 'tiktok_web',
            'browser_language': 'en-US',
            'browser_name': web_dev.get('browser_name', 'Mozilla'),
            'browser_online': 'true',
            'browser_platform': web_dev.get('browser_platform', 'Win32'),
            'browser_version': web_dev.get('browser_version', agent),
            'channel': 'tiktok_web',
            'cookie_enabled': 'true',
            'current_region': 'US',
            'device_id': _did,
            'device_platform': web_dev.get('device_platform', 'web_pc'),
            'focus_state': 'true',
            'from_page': 'user',
            'history_len': str(random.randint(3, 9)),
            'is_fullscreen': 'false',
            'is_page_visible': 'true',
            'lang': 'en',
            'nickname': target_name,
            'object_id': target_id,
            'owner_id': target_id,
            'priority_region': 'US',
            'reason': reason_code,
            'report_type': 'user',
            'reporter_id': web_cookies.get('user_id', web_cookies.get('uid_tt', '7694474723518858262')),
            'secUid': sec_uid,
            'target': target_id,
            'uniqueId': target_user,
            'user_is_login': 'true' if session_id else 'false',
            'webcast_language': 'en'
        }
        headers = {
            'User-Agent': agent,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': f'https://www.tiktok.com/@{target_user}',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        if web_dev.get('sec_ch_ua'):
            headers['Sec-Ch-Ua'] = web_dev['sec_ch_ua']
        if web_dev.get('sec_ch_ua_mobile'):
            headers['Sec-Ch-Ua-Mobile'] = web_dev['sec_ch_ua_mobile']
        if web_dev.get('sec_ch_ua_platform'):
            headers['Sec-Ch-Ua-Platform'] = web_dev['sec_ch_ua_platform']
        cookies = web_cookies if web_cookies else ({'sessionid': session_id} if session_id else None)
    else:
        url = 'https://api16-normal-c-alisg.ttapis.com/aweme/v2/aweme/feedback/'
        Mahos = pick_available_device()
        agent = Mahos['User-Agent']
        brand = Mahos['brand']
        dev_type = Mahos['type']
        device_full_name = f"{brand} {dev_type}"
        tim = '{:.0f}'.format(time.time() * 1000)
        _iid = '73' + ''.join(random.choices('0123456789', k=16))
        _did = '73' + ''.join(random.choices('0123456789', k=16))
        _udid = hexlify(randbytes(8)).decode()
        _cdid = str(uuid4())
        _ts = str(int(time.time()))
        _now = datetime.now().strftime('%H:%M:%S')

        save_registered_device({
            'registered_at': _now,
            'brand':         brand,
            'model':         dev_type,
            'device_id':     _did,
            'install_id':    _iid,
            'openudid':      _udid,
            'cdid':          _cdid,
        })

        params = {
            'owner_id':               target_id,
            'object_id':              target_id,
            'report_type':            'user',
            'enter_from':             'others_homepage',
            'isFirst':                '1',
            'no_hw':                  '1',
            'report_desc':            '',
            'uri':                    '',
            'reason':                 reason_code,
            'category':               category,
            'request_tag_from':       'h5',
            'iid':                    _iid,
            'device_id':              _did,
            'ac':                     'MOBILE_4G',
            'channel':                'googleplay',
            'aid':                    '1233',
            'app_name':               'musical_ly',
            'version_code':           '310503',
            'version_name':           '31.5.3',
            'device_platform':        'android',
            'os':                     'android',
            'ab_version':             '31.5.3',
            'ssmix':                  'a',
            'device_type':            dev_type,
            'device_brand':           brand,
            'language':               'ar',
            'os_api':                 '29',
            'os_version':             '10',
            'openudid':               _udid,
            'manifest_version_code':  '2023105030',
            'resolution':             '720*1491',
            'dpi':                    '320',
            'update_version_code':    '2023105030',
            '_rticket':               tim,
            'is_pad':                 '0',
            'current_region':         'YE',
            'app_type':               'normal',
            'mcc_mnc':                '42102',
            'timezone_name':          'Asia/Aden',
            'carrier_region_v2':      '421',
            'residence':              'YE',
            'app_language':           'ar',
            'carrier_region':         'YE',
            'ac2':                    'lte',
            'uoo':                    '1',
            'op_region':              'YE',
            'timezone_offset':        '10800',
            'build_number':           '31.5.3',
            'host_abi':               'arm64-v8a',
            'locale':                 'ar',
            'ts':                     _ts,
            'cdid':                   _cdid,
        }

        # Cryptographic signatures
        now_unix = int(time.time())
        now_ms = str(now_unix * 1000)
        query_str = urlencode(params)

        try:
            gorgon_sig = Gorgon(query_str, now_unix).get_value()['x-gorgon']
        except Exception:
            gorgon_sig = ''
        try:
            ladon_sig = Ladon.encrypt(now_unix, '1611921764', 1233)
        except Exception:
            ladon_sig = ''
        try:
            argus_sig = Argus.get_sign(queryhash=query_str, timestamp=now_unix)
        except Exception:
            argus_sig = ''

        headers = {
            'User-Agent':                agent,
            'sdk-version':               '2',
            'passport-sdk-version':      '19',
            'x-tt-dm-status':           'login=0;ct=0;rt=7',
            'x-vc-bdturing-sdk-version': '2.3.3.i18n',
            'x-tt-store-region':        'ye',
            'x-tt-store-region-src':    'did',
            'x-ss-dp':                  '1233',
            'x-ss-req-ticket':          now_ms,
            'x-khronos':                str(now_unix),
            'x-gorgon':                 gorgon_sig,
            'x-ladon':                  ladon_sig,
            'x-argus':                  argus_sig,
        }

        if session_id:
            headers['cookie'] = f"sessionid={session_id}; sid_tt={session_id}"
            params['reporter_id'] = web_cookies.get('user_id', web_cookies.get('uid_tt', '7694474723518858262'))
            params['user_is_login'] = '1'
            cookies = {'sessionid': session_id, 'sid_tt': session_id}
        else:
            cookies = None

    plog = project_logger
    request_id = plog.new_request_id() if plog else None
    if plog:
        try:
            plog.log_request(
                request_id, 'GET', url,
                params=params, headers=headers, cookies=cookies,
                target=target_user, target_id=target_id, mode=mode,
                reason_code=reason_code, reason_label=reason_label,
                category=category, device=device_full_name, device_id=_did,
            )
        except Exception:
            pass

    t_start = time.monotonic()
    res = make_request_with_proxy(proxy_manager, url, params, headers,
                                  cookies=cookies)
    latency = (time.monotonic() - t_start) * 1000

    # Proxy label — report the endpoint actually used for this attempt, not
    # the pool's "best" proxy (which made every row look identical).
    if proxy_manager:
        p_label = proxy_manager.last_label or '—'
    else:
        p_label = 'Direct (no proxy)'

    captcha_detected = False
    if res is None:
        success = False
        http_status = '—'
        server_code = '—'
        raw_snippet = '(no response)'
    else:
        http_status = str(res.status_code)
        try:
            j = res.json()
            server_code = str(j.get('status_code', '—'))
            data_obj = j.get('data') or {}
            need_captcha = data_obj.get('need_captcha')
            is_captcha = (
                (need_captcha is True)
                or (j.get('status_code') == 1000)
                or ('verify_center' in data_obj)
                or ('verify_center' in res.text)
                or ('bdturing' in res.text.lower())
            )
            if is_captcha:
                captcha_detected = True
            
            # TikTok mobile app returns status_code: 0 with skip_reason: 2 when submitted anonymously
            success = (
                (j.get('status_code') == 0 or 'skip_reason' in j or res.status_code == 200)
                and not captcha_detected
            )
        except Exception:
            server_code = '—'
            raw_lower = (res.text or '').lower()
            if 'verify_center' in raw_lower or 'bdturing' in raw_lower:
                captcha_detected = True
                success = False
            else:
                success = (res.status_code == 200)
        full_response = res.text if res is not None else ''
        raw_snippet = full_response[:120].replace('\n', ' ') if full_response else (f"HTTP {res.status_code} (OK)" if res else '(no response)')

    # Captcha handling
    if captcha_detected:
        with state_lock:
            state['stats']['captcha_hits'] += 1
            state['blocked_devices'][device_full_name] = (
                time.time() + BLOCK_DURATION
            )
            save_important_request({
                'type':        'CAPTCHA_TRIGGERED',
                'time':        _now,
                'device':      device_full_name,
                'device_id':   _did,
                'proxy':       p_label,
                'http_status': http_status,
                'response':    raw_snippet,
            })
        time.sleep(3.0)

    if plog:
        try:
            plog.log_response(
                request_id,
                status_code=res.status_code if res is not None else None,
                headers=dict(res.headers) if res is not None else {},
                body=res.text if res is not None else None,
                latency_ms=latency,
                error=None if res is not None else 'no response',
                success=success, server_code=server_code,
                captcha=captcha_detected, proxy=p_label,
                reason_code=reason_code, reason_label=reason_label,
            )
        except Exception:
            pass

    with state_lock:
        if success:
            state['stats']['accepted'] += 1
            save_important_request({
                'type':        'REPORT_ACCEPTED',
                'time':        _now,
                'target':      state['target'].get('username', ''),
                'target_id':   target_id,
                'reason':      reason_code,
                'device':      device_full_name,
                'device_id':   _did,
                'proxy':       p_label,
                'server_code': server_code,
            })
        else:
            state['stats']['rejected'] += 1

        score = calculate_request_score(success, http_status,
                                        server_code, latency)
        total = state['stats']['accepted'] + state['stats']['rejected']

        req_entry = {
            'num':           total,
            'request_id':    request_id,
            'time':          _now,
            'target':        state['target'].get('username', ''),
            'success':       success,
            'http_status':   http_status,
            'server_code':   server_code,
            'latency':       round(latency, 1),
            'score':         score,
            'brand':         brand,
            'device':        dev_type,
            'device_id':     _did,
            'iid':           _iid,
            'openudid':      _udid,
            'cdid':          _cdid,
            'reason':        reason_code,
            'vector':        reason_label,
            'category':      category,
            'proxy':         p_label,
            'ua_short':      (agent[:55] + '…') if len(agent) > 55 else agent,
            'snippet':       raw_snippet,
            'response_full': full_response,
            'captcha':       captcha_detected,
        }

        state['last_request'] = req_entry
        state['request_log'].appendleft(req_entry)

    # Save to attack log file
    log_attack_request(req_entry)

    # Emit to all connected clients
    socketio.emit('new_request', req_entry)


# ── Background Worker ─────────────────────────────────────────────────────────

def worker_loop():
    while not stop_event.is_set():
        if not state['running']:
            time.sleep(0.5)
            continue
        try:
            do_report()
            delay = state['config']['delay']
            if delay > 0:
                time.sleep(delay)
        except Exception as e:
            print(f'[Worker error] {e}')
            if project_logger:
                try:
                    project_logger.log_event(
                        'worker_error',
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                except Exception:
                    pass
            time.sleep(1.0)


# ── Stats emitter (background) ───────────────────────────────────────────────

def stats_emitter():
    while True:
        if state['running']:
            with state_lock:
                accepted = state['stats']['accepted']
                rejected = state['stats']['rejected']
                total = accepted + rejected
                rate = round(accepted / total * 100, 1) if total else 0.0
                captcha_hits = state['stats']['captcha_hits']
                start = state['stats']['start_time']
                blocked_count = sum(
                    1 for exp in state['blocked_devices'].values()
                    if time.time() < exp
                )

            if start:
                elapsed_s = int(time.time() - start)
                h = elapsed_s // 3600
                m = (elapsed_s % 3600) // 60
                s = elapsed_s % 60
                elapsed = f'{h:02d}:{m:02d}:{s:02d}'
            else:
                elapsed = '00:00:00'

            proxy_info = None
            proxy_pool = None
            if proxy_manager:
                proxy_info = proxy_manager.stats()
                proxy_pool = proxy_manager.snapshot()

            socketio.emit('stats_update', {
                'accepted':        accepted,
                'rejected':        rejected,
                'total':           total,
                'rate':            rate,
                'captcha_hits':    captcha_hits,
                'elapsed':         elapsed,
                'blocked_devices': blocked_count,
                'proxy_stats':     proxy_info,
                'proxy_pool':      proxy_pool,
            })
        time.sleep(0.5)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/reasons', methods=['GET'])
def api_reasons():
    reasons = []
    for k, (code, cat, label) in REPORT_REASONS.items():
        reasons.append({
            'key': k, 'code': code, 'category': cat, 'label': label,
        })
    return jsonify(reasons)


@app.route('/api/web-devices', methods=['GET'])
def api_web_devices():
    """Return available authentic web device profiles."""
    return jsonify({
        'status': 'ok',
        'devices': get_web_device_profiles()
    })


@app.route('/api/proxies', methods=['GET'])
def api_proxies():
    """Return every configured proxy with live stats for the proxy panel."""
    requested = (flask_request.args.get('file') or '').strip()
    live = proxy_manager is not None and not requested
    manager = proxy_manager if live else None
    if manager is None:
        used_file = requested or 'proxies.txt'
        try:
            manager = ProxyManager.from_file(used_file, validate=False)
        except Exception as exc:
            return jsonify({
                'status': 'ok',
                'stats': None,
                'proxies': [],
                'count': 0,
                'file': used_file,
                'msg': str(exc),
            })
    else:
        used_file = requested or state.get('config', {}).get('proxy_file') or 'proxies.txt'
    return jsonify({
        'status': 'ok',
        'stats': manager.stats(),
        'proxies': manager.snapshot(),
        'count': len(manager),
        'file': used_file,
    })


@app.route('/api/proxy/check', methods=['POST'])
def api_proxy_check():
    """Probe one proxy's current exit IP and latency."""
    body = flask_request.get_json(force=True) or {}
    proxy_file = (body.get('file') or 'proxies.txt').strip() or 'proxies.txt'
    try:
        index = int(body.get('index', 0))
    except (TypeError, ValueError):
        index = -1
    entries = load_proxy_file(proxy_file)
    if not (0 <= index < len(entries)):
        return jsonify({'status': 'error', 'msg': 'Proxy index out of range'}), 400

    entry = entries[index]
    started = time.monotonic()
    try:
        resp = tls_get(
            'https://api.ipify.org?format=json',
            proxies=entry.as_requests_proxy(),
            timeout=20,
        )
        latency = (time.monotonic() - started) * 1000.0
        try:
            ip = resp.json().get('ip')
        except Exception:
            ip = (resp.text or '').strip()[:64]
        return jsonify({
            'status': 'ok',
            'endpoint': entry.safe_repr(),
            'ip': ip,
            'latency_ms': round(latency, 1),
            'http_status': resp.status_code,
        })
    except Exception as exc:
        return jsonify({
            'status': 'error',
            'endpoint': entry.safe_repr(),
            'msg': f'{type(exc).__name__}: {exc}'[:240],
        })


@app.route('/api/sessions', methods=['GET', 'POST'])
def api_sessions():
    """Get active sessions or add and validate a new session."""
    if flask_request.method == 'POST':
        body = flask_request.get_json(force=True)
        raw_cookie = body.get('cookie', '').strip()
        label = body.get('label', '').strip() or f"Acc_{int(time.time())}"
        if not raw_cookie:
            return jsonify({'status': 'error', 'msg': 'No cookie provided'}), 400
        
        cookie_dict = redis_mgr.parse_cookie_string(raw_cookie)
        if not cookie_dict:
            return jsonify({'status': 'error', 'msg': 'Could not parse cookie string'}), 400

        # Validate session before saving
        is_valid, user_or_err = validate_tiktok_session(cookie_dict)
        if not is_valid:
            return jsonify({
                'status': 'error',
                'msg': f'TikTok session verification failed: {user_or_err}. Cookie is not active.'
            }), 400

        ok = save_stored_session(label, cookie_dict, label, verified_user=user_or_err)
        if ok:
            return jsonify({
                'status': 'ok',
                'msg': f'Session verified and saved for @{user_or_err} ({label})',
                'label': label,
                'user': user_or_err
            })
        return jsonify({'status': 'error', 'msg': 'Could not persist session'}), 500

    # GET active sessions
    sessions = get_all_stored_sessions()
    return jsonify({
        'status': 'ok',
        'redis_connected': redis_mgr.is_connected,
        'sessions': [
            {
                'identifier': s.get('identifier'),
                'label': s.get('label'),
                'status': s.get('status', 'unknown'),
                'sessionid': s.get('sessionid', ''),
                'user': s.get('verified_user') or s.get('user', ''),
                'added_at': s.get('added_at', 0),
                'last_verified': s.get('last_verified', 0),
            }
            for s in sessions
        ]
    })


@app.route('/api/sessions/delete', methods=['POST'])
def api_sessions_delete():
    """Delete a stored session by identifier or label."""
    body = flask_request.get_json(force=True) if flask_request.is_json else {}
    identifier = body.get('identifier', '').strip()
    if not identifier:
        return jsonify({'status': 'error', 'msg': 'No session identifier provided'}), 400
    ok = delete_stored_session(identifier)
    return jsonify({
        'status': 'ok' if ok else 'error',
        'msg': f"Session '{identifier}' deleted successfully" if ok else f"Could not find or delete '{identifier}'"
    })


@app.route('/api/sessions/validate', methods=['POST'])
def api_sessions_validate():
    """Manually test a session string, validate by identifier, check pool single, or check all saved sessions."""
    body = flask_request.get_json(force=True) if (flask_request.is_json and flask_request.data) else {}
    raw_cookie = body.get('cookie', '').strip() if body else ''
    identifier = body.get('identifier', '').strip() if body else ''

    # 1. Direct cookie string provided
    if raw_cookie:
        cookie_dict = redis_mgr.parse_cookie_string(raw_cookie)
        is_valid, user_or_err = validate_tiktok_session(cookie_dict)
        return jsonify({
            'status': 'ok' if is_valid else 'error',
            'valid': is_valid,
            'user': user_or_err if is_valid else None,
            'result': user_or_err,
            'msg': f"Active TikTok account: @{user_or_err}" if is_valid else str(user_or_err)
        })

    # 2. Specific stored session identifier provided
    if identifier and identifier not in ('pool', 'rotate', 'all', 'custom'):
        stored = get_all_stored_sessions()
        matched_sess = None
        for s in stored:
            if s.get('identifier') == identifier or s.get('label') == identifier:
                matched_sess = s
                break
        if not matched_sess:
            return jsonify({'status': 'error', 'valid': False, 'msg': f"Session '{identifier}' not found"}), 404

        c_dict = matched_sess.get('cookies_dict')
        if not c_dict and 'cookies' in matched_sess:
            try:
                c_dict = json.loads(matched_sess['cookies']) if isinstance(matched_sess['cookies'], str) else dict(matched_sess['cookies'])
            except Exception:
                c_dict = {}

        is_valid, user_or_err = validate_tiktok_session(c_dict) if c_dict else (False, 'No cookies in session')
        status_str = 'active' if is_valid else 'expired'
        update_stored_session_status(identifier, status_str, user_or_err if is_valid else '')

        return jsonify({
            'status': 'ok' if is_valid else 'error',
            'valid': is_valid,
            'identifier': identifier,
            'label': matched_sess.get('label', identifier),
            'user': user_or_err if is_valid else None,
            'result': user_or_err,
            'msg': f"Verified active for @{user_or_err}" if is_valid else f"Session check failed: {user_or_err}"
        })

    # 3. Check pool single session (used before starting attack in pool mode)
    if body.get('check_pool_single') or identifier in ('pool', 'rotate'):
        active_sess = pick_any_active_session()
        if not active_sess:
            return jsonify({
                'status': 'error',
                'valid': False,
                'msg': 'No saved sessions found in pool. Please add a TikTok session first.'
            }), 400
        c_dict = active_sess.get('cookies_dict')
        if not c_dict and 'cookies' in active_sess:
            try:
                c_dict = json.loads(active_sess['cookies']) if isinstance(active_sess['cookies'], str) else dict(active_sess['cookies'])
            except Exception:
                c_dict = {}
        is_valid, user_or_err = validate_tiktok_session(c_dict) if c_dict else (False, 'No cookies in session')
        ident = active_sess.get('identifier') or active_sess.get('label', 'pool')
        if not is_valid and ident:
            update_stored_session_status(ident, 'expired', '')
        elif is_valid and ident:
            update_stored_session_status(ident, 'active', user_or_err)
        return jsonify({
            'status': 'ok' if is_valid else 'error',
            'valid': is_valid,
            'identifier': ident,
            'label': active_sess.get('label', ident),
            'user': user_or_err if is_valid else None,
            'result': user_or_err,
            'msg': f"Pool session active for @{user_or_err}" if is_valid else f"Pool session check failed: {user_or_err}"
        })

    # 4. Batch validate all stored sessions
    stored = get_all_stored_sessions()
    results = []
    for s in stored:
        c_dict = s.get('cookies_dict')
        if not c_dict and 'cookies' in s and isinstance(s['cookies'], str):
            try:
                c_dict = json.loads(s['cookies'])
            except Exception:
                c_dict = {}
        is_valid, user_or_err = validate_tiktok_session(c_dict) if c_dict else (False, 'Missing cookies')
        ident = s.get('identifier') or s.get('label')
        if ident:
            update_stored_session_status(ident, 'active' if is_valid else 'expired', user_or_err if is_valid else '')
        results.append({
            'identifier': ident,
            'label': s.get('label'),
            'valid': is_valid,
            'user': user_or_err if is_valid else None,
            'info': user_or_err
        })
    return jsonify({'status': 'ok', 'results': results})


@app.route('/api/lookup', methods=['POST'])
def api_lookup():
    body = flask_request.get_json(force=True)
    username = body.get('username', '').strip().lstrip('@')
    if not username:
        return jsonify({'status': 'bad', 'error': 'No username provided'}), 400
    info = get_tiktok_info(username, proxy_mgr=proxy_manager)
    if info.get('status') == 'ok':
        with state_lock:
            state['target'] = info
        save_last_target(username)
    else:
        # Check historical cache
        cached = find_cached_target(username)
        if cached and cached.get('id'):
            cached_info = {
                'id': str(cached['id']),
                'name': cached.get('name') or username,
                'sec_uid': cached.get('sec_uid', ''),
                'username': username,
                'followers': cached.get('followers', 0),
                'following': cached.get('following', 0),
                'likes': cached.get('likes', 0),
                'country': cached.get('country', '—'),
                'private': 'unknown',
                'bio': cached.get('bio', ''),
                'date': cached.get('last_targeted_at', '—'),
                'status': 'ok',
                'cached': True,
                'warning': info.get('error', 'Live profile unavailable; loaded from historical telemetry')
            }
            with state_lock:
                state['target'] = cached_info
            save_last_target(username)
            return jsonify(cached_info)
    return jsonify(info)


@app.route('/api/start', methods=['POST'])
def api_start():
    global proxy_manager, project_logger

    if state['running']:
        return jsonify({'status': 'error', 'msg': 'Already running'}), 400

    if not state['target'].get('id'):
        return jsonify({
            'status': 'error',
            'msg': 'Look up a target first',
        }), 400

    body = flask_request.get_json(force=True)

    # Apply config — one or more report reasons, rotated per request
    reason_entries = parse_report_reasons(body)
    reason_keys = [entry[0] for entry in reason_entries]
    reasons_payload = [
        {'key': key, 'code': rc, 'category': rcat, 'label': rlabel}
        for key, rc, rcat, rlabel in reason_entries
    ]
    code, cat, label = reason_entries[0][1], reason_entries[0][2], reason_entries[0][3]

    delay = max(0.0, float(body.get('delay', 1.0)))
    use_proxy = bool(body.get('use_proxy', False))
    proxy_file = body.get('proxy_file', 'proxies.txt') or 'proxies.txt'
    session_id = body.get('session_id', '') or ''
    mode = str(body.get('mode', 'mobile')).lower()
    if mode not in ('mobile', 'web'):
        mode = 'mobile'

    # Parse cookies if provided
    raw_cookies = body.get('cookies') or ''
    web_cookies = {}
    if isinstance(raw_cookies, dict):
        web_cookies = dict(raw_cookies)
    elif isinstance(raw_cookies, str) and raw_cookies.strip():
        web_cookies = redis_mgr.parse_cookie_string(raw_cookies)

    # If specific session selected from dropdown, load its cookies
    session_identifier = body.get('session_identifier', '') or body.get('session_id', '')
    if mode == 'web' and not web_cookies and session_identifier and session_identifier not in ('pool', 'rotate', ''):
        for s in get_all_stored_sessions():
            if s.get('identifier') == session_identifier or s.get('label') == session_identifier:
                c_dict = s.get('cookies_dict')
                if not c_dict and 'cookies' in s:
                    try:
                        c_dict = json.loads(s['cookies']) if isinstance(s['cookies'], str) else dict(s['cookies'])
                    except Exception:
                        c_dict = {}
                if c_dict:
                    web_cookies = dict(c_dict)
                    session_id = s.get('sessionid') or web_cookies.get('sessionid', '')
                break

    # If no session cookies provided yet, automatically load from Redis or local saved sessions
    if mode == 'web' and not web_cookies:
        active_sess = pick_any_active_session()
        if active_sess:
            web_cookies = dict(active_sess.get('cookies_dict') or {})
            if not session_id:
                session_id = active_sess.get('sessionid', '')

    if not session_id and 'sessionid' in web_cookies:
        session_id = web_cookies['sessionid']

    # When mobile mode is selected, strictly avoid using any sessions/cookies (use old method)
    if mode == 'mobile':
        session_id = ''
        web_cookies = {}
    elif mode == 'web':
        # Mandatory Session Validation for Web Mode
        if not web_cookies or not ('sessionid' in web_cookies or 'sessionid_ss' in web_cookies):
            return jsonify({
                'status': 'error',
                'msg': 'No TikTok session found. Web mode requires an active TikTok login cookie/sessionid. Please add a session or switch to Mobile mode.'
            }), 400

        is_valid, user_or_err = validate_tiktok_session(web_cookies)
        if not is_valid:
            return jsonify({
                'status': 'error',
                'msg': f'TikTok session check failed: {user_or_err}. Please refresh your browser login cookie before starting.'
            }), 400

    web_device = str(body.get('web_device', 'random')).strip() or 'random'

    with state_lock:
        state['config'].update({
            'mode':         mode,
            'reason_code':  code,
            'category':     cat,
            'reason_label': label,
            'reason_keys':  reason_keys,
            'reasons':      reasons_payload,
            'session_id':   session_id,
            'web_cookies':  web_cookies,
            'web_device':   web_device,
            'delay':        delay,
            'use_proxy':    use_proxy,
            'proxy_file':   proxy_file,
        })
        state['stats'] = {
            'accepted':     0,
            'rejected':     0,
            'captcha_hits': 0,
            'start_time':   time.time(),
        }
        state['reason_index'] = 0
        state['request_log'].clear()
        state['last_request'] = {}
        state['blocked_devices'] = {}

    # Proxy setup
    if use_proxy:
        try:
            proxy_manager = ProxyManager.from_file(proxy_file, validate=False)
        except Exception as e:
            proxy_manager = None
            return jsonify({
                'status': 'error',
                'msg': f'Proxy setup failed: {e}',
            }), 400
    else:
        proxy_manager = None

    # Initialize attack log file for this session
    global current_log_file
    target_uname = state['target'].get('username', 'target')
    timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    current_log_file = os.path.join(LOGS_DIR, f"attack_{target_uname}_{timestamp_str}.jsonl")

    run_config = {
        'mode': mode,
        'reason_code': code,
        'category': cat,
        'reason_label': label,
        'reason_keys': reason_keys,
        'reasons': reasons_payload,
        'web_device': web_device,
        'delay': delay,
        'use_proxy': use_proxy,
        'proxy_file': proxy_file,
    }

    # Detailed per-project streams (requests / responses / events)
    try:
        if project_logger is not None:
            project_logger.close()
    except Exception:
        pass
    try:
        project_logger = ProjectLogger(target_uname)
        project_logger.log_event(
            'attack_started',
            target=state['target'],
            config=run_config,
            summary_log=os.path.basename(current_log_file),
        )
    except Exception:
        project_logger = None

    with log_file_lock:
        try:
            with open(current_log_file, 'w', encoding='utf-8') as f:
                header = {
                    'event': 'ATTACK_STARTED',
                    'timestamp': datetime.now().isoformat(),
                    'target': state['target'],
                    'config': run_config,
                }
                f.write(json.dumps(header, ensure_ascii=False) + '\n')
        except Exception:
            pass

    # Start worker
    stop_event.clear()
    state['running'] = True
    state['workers'] = []
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    state['workers'].append(t)

    socketio.emit('status_change', {'running': True, 'log_file': os.path.basename(current_log_file)})

    if telegram_bot:
        try:
            telegram_bot.start_time = state['stats']['start_time']
            telegram_bot.broadcast(
                f"🚀 <b>THUNDER Dashboard Attack Started!</b>\n"
                f"🎯 <b>Target:</b> <code>@{state['target'].get('username')}</code>\n"
                f"⚙️ <b>Mode:</b> <code>{mode.upper()}</code> | <b>Delay:</b> <code>{delay}s</code>\n\n"
                f"👉 Send <code>/status</code> anytime to get live requests and runtime!"
            )
        except Exception:
            pass

    return jsonify({
        'status': 'ok',
        'log_file': os.path.basename(current_log_file),
        'project_log_dir': project_logger.path if project_logger else None,
    })


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global current_log_file
    stop_event.set()
    state['running'] = False

    if telegram_bot:
        try:
            total_reqs = state['stats']['accepted'] + state['stats']['rejected']
            telegram_bot.broadcast(
                f"⏹ <b>Attack Stopped from Dashboard.</b>\n"
                f"📊 <b>Summary:</b> Total: <code>{total_reqs}</code> | Accepted: <code>{state['stats']['accepted']}</code> | Rejected: <code>{state['stats']['rejected']}</code>"
            )
        except Exception:
            pass

    if current_log_file:
        with log_file_lock:
            try:
                with open(current_log_file, 'a', encoding='utf-8') as f:
                    footer = {
                        'event': 'ATTACK_STOPPED',
                        'timestamp': datetime.now().isoformat(),
                        'stats': dict(state['stats']),
                    }
                    f.write(json.dumps(footer, ensure_ascii=False) + '\n')
            except Exception:
                pass

    if project_logger:
        try:
            stats = dict(state['stats'])
            project_logger.log_event('attack_stopped', stats=stats)
            project_logger.close(stats=stats)
        except Exception:
            pass

    socketio.emit('status_change', {'running': False})
    return jsonify({'status': 'ok'})


@app.route('/api/state', methods=['GET'])
def api_state():
    with state_lock:
        accepted = state['stats']['accepted']
        rejected = state['stats']['rejected']
        total = accepted + rejected
        rate = round(accepted / total * 100, 1) if total else 0.0
        start = state['stats']['start_time']
        if start and state['running']:
            el = int(time.time() - start)
            elapsed = f'{el // 3600:02d}:{(el % 3600) // 60:02d}:{el % 60:02d}'
        else:
            elapsed = '00:00:00'

        return jsonify({
            'running':         state['running'],
            'target':          state['target'],
            'config':          state['config'],
            'accepted':        accepted,
            'rejected':        rejected,
            'total':           total,
            'rate':            rate,
            'captcha_hits':    state['stats']['captcha_hits'],
            'elapsed':         elapsed,
            'blocked_devices': sum(
                1 for exp in state['blocked_devices'].values()
                if time.time() < exp
            ),
            'last_request':    state['last_request'],
            'request_log':     list(state['request_log']),
            'proxy_stats':     proxy_manager.stats() if proxy_manager else None,
            'proxy_pool':      proxy_manager.snapshot() if proxy_manager else None,
            'current_log':     os.path.basename(current_log_file) if current_log_file else None,
            'project_log_dir': project_logger.path if project_logger else None,
        })


@app.route('/api/requests', methods=['GET'])
def api_requests():
    with state_lock:
        return jsonify(list(state['request_log']))


@app.route('/api/logs', methods=['GET'])
def api_logs_list():
    """List all saved attack log files with metadata."""
    try:
        files = []
        if os.path.exists(LOGS_DIR):
            for fname in sorted(os.listdir(LOGS_DIR), reverse=True):
                if fname.endswith('.jsonl') or fname.endswith('.json'):
                    fpath = os.path.join(LOGS_DIR, fname)
                    st = os.stat(fpath)
                    files.append({
                        'filename': fname,
                        'size': st.st_size,
                        'size_kb': round(st.st_size / 1024, 1),
                        'created': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        'is_current': (fpath == current_log_file)
                    })
        return jsonify({
            'status': 'ok',
            'logs': files,
            'current_log': os.path.basename(current_log_file) if current_log_file else None
        })
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/logs/download/<path:filename>', methods=['GET'])
def api_logs_download(filename):
    """Download a specific attack log file."""
    from flask import send_from_directory, abort
    safe_name = os.path.basename(filename)
    file_path = os.path.join(LOGS_DIR, safe_name)
    if not os.path.isfile(file_path):
        return jsonify({'status': 'error', 'msg': 'Log file not found'}), 404
    return send_from_directory(
        LOGS_DIR,
        safe_name,
        as_attachment=True,
        download_name=safe_name,
        mimetype='application/json' if safe_name.endswith('.json') or safe_name.endswith('.jsonl') else 'application/octet-stream'
    )


# ── Bootstrap ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start background stats emitter
    emitter = threading.Thread(target=stats_emitter, daemon=True)
    emitter.start()

    if telegram_bot:
        telegram_bot.start()
        print('  🤖 Telegram Bot remote monitor active (@q7nullveil_bot)')

    print('\n  ⚡ THUNDER Dashboard starting...')
    print('  ⚡ Open http://localhost:5000 in your browser\n')
    socketio.run(app, host='0.0.0.0', port=5000, debug=False,
                 allow_unsafe_werkzeug=True)
