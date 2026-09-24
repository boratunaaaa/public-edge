"""Telegram delivery. The token is read from the environment only and never logged."""
from __future__ import annotations

import logging
import re

import httpx

from app.config import settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def _redact(s: str) -> str:
    tok = settings.telegram_bot_token
    if tok:
        s = s.replace(tok, "<redacted>")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot<redacted>", s)


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id and settings.telegram_enabled)


def _call(method: str, payload: dict) -> dict:
    if not configured():
        raise TelegramError("telegram not configured")
    try:
        r = httpx.post(API.format(token=settings.telegram_bot_token, method=method), json=payload, timeout=20)
        data = r.json()
    except Exception as e:  # never let the token leak through exception text
        raise TelegramError(_redact(f"{type(e).__name__}: {e}")) from None
    if not data.get("ok"):
        raise TelegramError(_redact(f"{r.status_code} {data.get('description')}"))
    return data["result"]


def check() -> dict:
    """Startup test: token valid (getMe) + chat reachable (getChat). Sends nothing."""
    me = _call("getMe", {})
    chat = _call("getChat", {"chat_id": settings.telegram_chat_id})
    return {"bot_username": me.get("username"), "chat_type": chat.get("type"), "chat_title": chat.get("title")}


def send(text: str) -> None:
    _call("sendMessage", {"chat_id": settings.telegram_chat_id, "text": text[:4000], "parse_mode": "HTML",
                          "disable_web_page_preview": True})


def send_photo(png: bytes, caption: str = "") -> None:
    if not configured():
        raise TelegramError("telegram not configured")
    try:
        r = httpx.post(API.format(token=settings.telegram_bot_token, method="sendPhoto"),
                       data={"chat_id": settings.telegram_chat_id, "caption": caption[:1000]},
                       files={"photo": ("chart.png", png, "image/png")}, timeout=30)
        data = r.json()
    except Exception as e:
        raise TelegramError(_redact(f"{type(e).__name__}: {e}")) from None
    if not data.get("ok"):
        raise TelegramError(_redact(f"{r.status_code} {data.get('description')}"))
