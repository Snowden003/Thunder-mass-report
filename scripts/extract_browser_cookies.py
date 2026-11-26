#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser TikTok Cookie Extractor & Session Importer.
Extracts authenticated TikTok cookies directly from local browsers (Chrome, Chromium, Firefox),
decrypts them, verifies session validity with TikTok Passport, and saves them to Redis.
"""

import os
import sys
import glob
import shutil
import sqlite3
import tempfile
import hashlib
import requests
from typing import Dict, Optional, Tuple
from Crypto.Cipher import AES

try:
    from core.redis_manager import RedisSessionManager
except ImportError:
    RedisSessionManager = None

def _decrypt_chrome_v10(enc: bytes, password: bytes = b'peanuts') -> str:
    """Decrypt Chrome Linux v10/v11 encrypted cookie bytes."""
    if not enc:
        return ""
    if enc.startswith(b'v10') or enc.startswith(b'v11'):
        salt = b'saltysalt'
        iv = b' ' * 16
        key = hashlib.pbkdf2_hmac('sha1', password, salt, 1, 16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        raw = cipher.decrypt(enc[3:])
        # Strip PKCS7 padding
        pad = raw[-1]
        if 1 <= pad <= 16:
            raw = raw[:-pad]
        # Cookie plaintext starts at byte offset 32 (after metadata header)
        return raw[32:].decode('utf-8', errors='ignore')
    return enc.decode('utf-8', errors='ignore')

def extract_from_chrome(profile_dir: Optional[str] = None) -> Dict[str, str]:
    """Extract and decrypt TikTok cookies from Google Chrome or Chromium."""
    if not profile_dir:
        candidates = [
            os.path.expanduser('~/.config/google-chrome/Default/Cookies'),
            os.path.expanduser('~/.config/chromium/Default/Cookies'),
            *glob.glob(os.path.expanduser('~/.config/google-chrome/Profile */Cookies')),
            *glob.glob(os.path.expanduser('~/.config/chromium/Profile */Cookies')),
        ]
        db_path = next((c for c in candidates if os.path.exists(c)), None)
    else:
        db_path = profile_dir

    if not db_path or not os.path.exists(db_path):
        return {}

    # Copy to temporary file to bypass file lock if browser is open
    tmp = tempfile.NamedTemporaryFile(delete=False).name
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%tiktok.com%'")
        cookies = {}
        for name, enc in cur.fetchall():
            val = _decrypt_chrome_v10(enc)
            if val:
                cookies[name] = val
        conn.close()
        return cookies
    except Exception as e:
        print(f"[!] Error reading Chrome cookies: {e}", file=sys.stderr)
        return {}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def extract_from_firefox() -> Dict[str, str]:
    """Extract unencrypted TikTok cookies from Firefox profiles."""
    profiles = glob.glob(os.path.expanduser('~/.mozilla/firefox/*.*/cookies.sqlite'))
    if not profiles:
        return {}

    cookies = {}
    for db_path in profiles:
        tmp = tempfile.NamedTemporaryFile(delete=False).name
        try:
            shutil.copy2(db_path, tmp)
            conn = sqlite3.connect(tmp)
            cur = conn.cursor()
            cur.execute("SELECT name, value FROM moz_cookies WHERE host LIKE '%tiktok.com%'")
            for name, val in cur.fetchall():
                if val and name not in cookies:
                    cookies[name] = val
            conn.close()
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return cookies

def verify_tiktok_session(cookies: Dict[str, str]) -> Tuple[bool, dict]:
    """Verify cookies with TikTok Passport service and return account metadata."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    url = 'https://www.tiktok.com/passport/web/account/info/?aid=1988'
    try:
        r = requests.get(url, cookies=cookies, headers=headers, timeout=10)
        data = r.json()
        if data.get('message') == 'success' and 'data' in data:
            return True, data['data']
        return False, data.get('data', {})
    except Exception as e:
        return False, {'error': str(e)}

def main():
    print("\033[1;36m[»] Scanning local browsers for TikTok sessions...\033[0m")
    
    # 1. Try Chrome first
    chrome_cookies = extract_from_chrome()
    if chrome_cookies and 'sessionid' in chrome_cookies:
        print(f"\033[1;32m[✔] Found {len(chrome_cookies)} TikTok cookies in Google Chrome!\033[0m")
        ok, account_data = verify_tiktok_session(chrome_cookies)
        if ok:
            print("\n\033[1;32m╔══════════════════════════════════════════════════════════════╗\033[0m")
            print("\033[1;32m║               TIKTOK ACCOUNT DETAILS FOUND                   ║\033[0m")
            print("\033[1;32m╚══════════════════════════════════════════════════════════════╝\033[0m")
            print(f"  • \033[1;97mUsername     :\033[0m \033[1;33m@{account_data.get('username')}\033[0m")
            print(f"  • \033[1;97mScreen Name  :\033[0m \033[1;36m{account_data.get('screen_name')}\033[0m")
            print(f"  • \033[1;97mUser ID      :\033[0m \033[1;97m{account_data.get('user_id')}\033[0m")
            print(f"  • \033[1;97msec_user_id  :\033[0m \033[38;5;245m{account_data.get('sec_user_id')}\033[0m")
            print(f"  • \033[1;97mPhone        :\033[0m \033[1;97m{account_data.get('mobile', '—')}\033[0m")
            print(f"  • \033[1;97mEmail        :\033[0m \033[1;97m{account_data.get('email', '—')}\033[0m")
            print(f"  • \033[1;97mCountry Code :\033[0m \033[1;97m+{account_data.get('country_code')}\033[0m")
            
            # Save to Redis
            if RedisSessionManager:
                mgr = RedisSessionManager()
                username = account_data.get('username') or f"user_{account_data.get('user_id')}"
                if mgr.is_connected:
                    mgr.save_session(
                        username,
                        chrome_cookies,
                        username,
                        user_id=str(account_data.get('user_id', '')),
                        sec_user_id=str(account_data.get('sec_user_id', '')),
                        screen_name=str(account_data.get('screen_name', username)),
                        verified_user=username,
                        email=str(account_data.get('email', '')),
                        mobile=str(account_data.get('mobile', '')),
                        last_status='HTTP 200 (Active)',
                    )
                    print(f"\n\033[1;32m[✔] Account @{username} successfully saved to Redis Cloud with full metadata!\033[0m")
            return

    # 2. Try Firefox
    ff_cookies = extract_from_firefox()
    if ff_cookies and 'sessionid' in ff_cookies:
        print(f"\033[1;32m[✔] Found {len(ff_cookies)} TikTok cookies in Firefox!\033[0m")
        ok, account_data = verify_tiktok_session(ff_cookies)
        if ok:
            username = account_data.get('username') or f"user_{account_data.get('user_id')}"
            print(f"\033[1;32m[✔] Verified account: @{username} (ID: {account_data.get('user_id')})\033[0m")
            if RedisSessionManager:
                mgr = RedisSessionManager()
                if mgr.is_connected:
                    mgr.save_session(
                        username,
                        ff_cookies,
                        username,
                        user_id=str(account_data.get('user_id', '')),
                        sec_user_id=str(account_data.get('sec_user_id', '')),
                        screen_name=str(account_data.get('screen_name', username)),
                        verified_user=username,
                        email=str(account_data.get('email', '')),
                        mobile=str(account_data.get('mobile', '')),
                        last_status='HTTP 200 (Active)',
                    )
                    print(f"\n\033[1;32m[✔] Account @{username} successfully saved to Redis Cloud with full metadata!\033[0m")
            return

    print("\033[1;31m[✗] No active signed-in TikTok sessions found in local browsers.\033[0m")

if __name__ == '__main__':
    main()
