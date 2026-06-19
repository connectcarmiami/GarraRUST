#!/usr/bin/env python3
"""notify — Telegram delivery for Garra's monitor/heartbeat and health checks.

Sends ONLY to authorized chats (owner from allowlist.json by default). Reads the
bot token from ~/.config/garraia/config.yml. Pure stdlib.
"""
import json
import os
import re
import urllib.parse
import urllib.request

CONFIG = "/home/connect-car/.config/garraia/config.yml"
ALLOWLIST = "/home/connect-car/.config/garraia/allowlist.json"
TIMEOUT = 15


def get_token():
    txt = open(CONFIG).read()
    m = re.search(r"bot_token:\s*(\S+)", txt)
    return m.group(1) if m else None


def owner_chat():
    try:
        d = json.load(open(ALLOWLIST))
        return str(d.get("owner") or (d.get("users") or [None])[0])
    except Exception:
        return None


def authorized_chats():
    try:
        d = json.load(open(ALLOWLIST))
        return {str(u) for u in d.get("users", [])} | ({str(d["owner"])} if d.get("owner") else set())
    except Exception:
        return set()


def _api(method, params=None):
    token = get_token()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_me():
    return _api("getMe")


_BOT_ID_CACHE = []


def bot_id():
    """Telegram bot id (numeric), cached. None if unreachable."""
    if _BOT_ID_CACHE:
        return _BOT_ID_CACHE[0]
    try:
        me = get_me()
        bid = (me.get("result") or {}).get("id")
        if bid is not None:
            _BOT_ID_CACHE.append(str(bid))
            return _BOT_ID_CACHE[0]
    except Exception:
        pass
    return None


def mask_chat(chat_id):
    """Mask a chat/destination id, keeping only the last 4 chars."""
    s = str(chat_id or "")
    return ("*" * max(0, len(s) - 4)) + s[-4:] if s else "(desconhecido)"


def mask_chat_audit(chat_id, keep=7):
    """Audit-friendly mask keeping the FIRST `keep` chars + '***' (ex.:
    7978617919 -> 7978617***). Hides the tail; never a secret/token."""
    s = str(chat_id or "")
    if not s:
        return None
    if len(s) <= keep:
        return s  # already short; nothing meaningful to hide
    return s[:keep] + "***"


def send_message(chat_id, text, only_authorized=True):
    """Send a Telegram message and return a STRUCTURED delivery result.

    Returns {ok, message_id, chat_masked, error, result}. ``ok`` is True only
    when Telegram confirmed delivery; ``message_id`` is the channel's real id
    (None on failure). Callers persist these via taskstore.record_notification —
    nothing is ever reported "delivered" without a real message_id here.
    """
    chat_id = str(chat_id)
    masked = mask_chat(chat_id)
    if only_authorized and chat_id not in authorized_chats():
        return {"ok": False, "message_id": None, "chat_masked": masked,
                "error": f"chat {masked} não autorizado"}
    try:
        raw = _api("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                                   "disable_web_page_preview": "true"})
    except Exception as e:  # network / HTTP error
        return {"ok": False, "message_id": None, "chat_masked": masked, "error": str(e)[:300]}
    ok = bool(raw.get("ok"))
    result = raw.get("result") or {}
    mid = result.get("message_id") if ok else None
    # Real chat the message landed in — used to detect chat_mismatch before
    # ever recording a notification as "delivered".
    delivered_chat_id = str((result.get("chat") or {}).get("id")) if ok and result.get("chat") else None
    return {"ok": ok, "message_id": mid, "chat_masked": masked,
            "delivered_chat_id": delivered_chat_id,
            "error": None if ok else str(raw.get("description") or raw)[:300],
            "result": raw}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "me":
        print(json.dumps(get_me()))
    elif len(sys.argv) > 2 and sys.argv[1] == "send":
        print(json.dumps(send_message(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "test")))
    else:
        print("owner:", owner_chat(), "authorized:", authorized_chats())
