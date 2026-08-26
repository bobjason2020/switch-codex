#!/usr/bin/env python3
"""Switch-codex 管理认证与会话。

管理端使用 PBKDF2 密码哈希 + SQLite 持久化会话；客户端走 Router master key。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from sy import core, db
from sy.const import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_MASTER_KEY,
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_MINUTES,
    SESSION_TTL_DAYS,
)

log = logging.getLogger("switchyard.auth")

SESSION_TTL = timedelta(days=SESSION_TTL_DAYS)
LOGIN_WINDOW = timedelta(minutes=LOGIN_WINDOW_MINUTES)

_sessions: dict[str, datetime] = {}
_sessions_lock = threading.Lock()
_login_failures: dict[str, list[datetime]] = {}
_login_failures_lock = threading.Lock()


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    iterations = 310000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def load_auth() -> dict:
    return db.load_auth_raw()


def save_auth(auth: dict) -> None:
    db.save_auth_raw(auth)


def ensure_auth() -> dict:
    auth = load_auth()
    if not auth.get("password_hash"):
        auth = {
            "password_hash": _hash_password(DEFAULT_ADMIN_PASSWORD),
            "must_change": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_auth(auth)
        log.warning("auth initialized with DEFAULT password; change it on first login")
    return auth


def default_password_active() -> bool:
    auth = ensure_auth()
    return _verify_password(DEFAULT_ADMIN_PASSWORD, auth.get("password_hash", ""))


def _new_session() -> str:
    token = secrets.token_hex(32)
    expires = datetime.now() + SESSION_TTL
    try:
        db.save_admin_session(token, expires.isoformat(timespec="seconds"))
    except Exception:
        log.exception("persist admin session failed")
    with _sessions_lock:
        _sessions[token] = expires
    return token


def restore_admin_sessions() -> None:
    now = datetime.now()
    try:
        db.purge_expired_admin_sessions(now.isoformat(timespec="seconds"))
        rows = db.load_admin_sessions()
        with _sessions_lock:
            _sessions.clear()
            for token, expires_at in rows:
                try:
                    exp = datetime.fromisoformat(expires_at)
                except (TypeError, ValueError):
                    exp = None
                if exp is not None and exp > now:
                    _sessions[token] = exp
                else:
                    db.delete_admin_session(token)
        log.info("restored admin sessions count=%s", len(_sessions))
    except Exception:
        log.exception("restore admin sessions failed")


def _session_valid(token: str) -> bool:
    now = datetime.now()
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is not None and exp <= now:
            _sessions.pop(token, None)
    if exp is not None:
        if exp <= now:
            try:
                db.delete_admin_session(token)
            except Exception:
                log.exception("delete expired admin session failed")
            return False
        return True
    try:
        rows = db.load_admin_sessions()
    except Exception:
        log.exception("load admin sessions failed")
        rows = []
    found = next((e for t, e in rows if t == token), None)
    if found is None:
        return False
    try:
        exp = datetime.fromisoformat(found)
    except (TypeError, ValueError):
        db.delete_admin_session(token)
        return False
    if exp <= now:
        db.delete_admin_session(token)
        return False
    with _sessions_lock:
        _sessions[token] = exp
    return True


def _login_blocked(client_ip: str) -> bool:
    with _login_failures_lock:
        now = datetime.now()
        hits = [t for t in _login_failures.get(client_ip, []) if now - t < LOGIN_WINDOW]
        _login_failures[client_ip] = hits
        return len(hits) >= LOGIN_MAX_FAILURES


def _record_login_failure(client_ip: str) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(client_ip, []).append(datetime.now())


def _record_login_success(client_ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(client_ip, None)


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization.split(" ", 1)[1].strip()


def require_session(authorization: Optional[str] = Header(None)) -> str:
    token = _bearer_token(authorization)
    if not token or not _session_valid(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return token


def require_master(authorization: Optional[str] = Header(None)) -> str:
    token = require_session(authorization)
    if ensure_auth().get("must_change"):
        raise HTTPException(status_code=403, detail="首次登录必须先修改默认密码")
    return token


def _ensure_master_key() -> str:
    """空/缺失 master_key 时回写内置默认值，不生成新 key。"""
    cfg = core.load_config()
    master = str(cfg.get("master_key") or "").strip()
    if master:
        return master
    cfg["master_key"] = DEFAULT_MASTER_KEY
    try:
        core.save_config(cfg)
    except Exception:
        log.exception("persist default master_key failed")
    return DEFAULT_MASTER_KEY


def _keys_equal(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not left or not right:
        return False
    if len(left) != len(right):
        secrets.compare_digest(right, right)
        return False
    return secrets.compare_digest(left, right)


def require_client_key(authorization: Optional[str] = Header(None)) -> str:
    master = _ensure_master_key()
    token = _bearer_token(authorization)
    if not token or not _keys_equal(token, master):
        raise HTTPException(status_code=401, detail="Invalid client key")
    return token


def revoke_session(token: str) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)
    try:
        db.delete_admin_session(token)
    except Exception:
        log.exception("revoke admin session failed")


def revoke_all_sessions() -> None:
    with _sessions_lock:
        _sessions.clear()
    try:
        db.delete_all_admin_sessions()
    except Exception:
        log.exception("revoke all admin sessions failed")


def login_client_ip(request) -> str:
    """Return the rate-limit address using the configured proxy-header policy."""
    public = core.load_public_config()
    if public.get("trust_proxy_headers"):
        try:
            for header in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip"):
                value = (request.headers.get(header) or "").strip()
                if value:
                    return value.split(",")[0].strip()
        except Exception:
            pass
    client = getattr(request, "client", None)
    return client.host if client else ""


class LoginIn(BaseModel):
    password: str = Field(..., min_length=1)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)
