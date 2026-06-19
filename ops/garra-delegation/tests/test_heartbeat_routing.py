#!/usr/bin/env python3
"""Acceptance suite for the heartbeat/notification chat-routing fix.

Hermetic: throwaway tasks DB + stubbed Telegram adapter. Proves notifications go
to the conversation's REAL origin chat (not a hardcoded owner), the owner is only
a fallback, routing fields are persisted, and a chat_mismatch never records as
delivered.
"""
import os
import sys
import tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="hb_route_", suffix=".db", delete=False)
_tmp.close()
os.environ["GARRA_TASKS_DB"] = _tmp.name

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts, notify, monitor   # noqa: E402
import delegation_mcp as dm                                      # noqa: E402

MICHEL = "7978617919"
OWNER = "8890643175"

# Stub the Telegram side: both chats authorized; owner = OWNER; fixed bot id.
notify.authorized_chats = lambda: {MICHEL, OWNER}
notify.owner_chat = lambda: OWNER
notify.bot_id = lambda: "8940907680"

_fail = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        _fail.append(name)


def main():
    ts.init_db()

    # 1 — origin = Michel's chat -> deliver to Michel, scope=origin
    r = dm._resolve_routing(MICHEL)
    check("1 origem 7978617919 -> notify 7978617919, scope origin",
          r["notify_chat_id"] == MICHEL and r["delivery_scope"] == "origin"
          and r["origin_chat_id"] == MICHEL)

    # 2 — origin = owner chat -> deliver to owner, scope=origin (still origin-driven)
    r = dm._resolve_routing(OWNER)
    check("2 origem 8890643175 -> notify 8890643175, scope origin",
          r["notify_chat_id"] == OWNER and r["delivery_scope"] == "origin")

    # 3 — no origin -> owner fallback
    r = dm._resolve_routing("")
    check("3 sem origem -> owner, scope fallback_owner",
          r["notify_chat_id"] == OWNER and r["delivery_scope"] == "fallback_owner"
          and r["origin_chat_id"] is None)

    # 3b — origin not authorized -> fallback (no delivery to arbitrary chats)
    r = dm._resolve_routing("123000999")
    check("3b origem não autorizada -> fallback_owner",
          r["notify_chat_id"] == OWNER and r["delivery_scope"] == "fallback_owner")

    # 4 — create_task persists all routing fields
    r = dm._resolve_routing(MICHEL)
    t, _ = ts.create_task("garra", "flash", "engineering", "rota persist 1",
                          notify_chat_id=r["notify_chat_id"], origin_chat_id=r["origin_chat_id"],
                          origin_channel=r["origin_channel"], bot_id=r["bot_id"],
                          message_thread_id=r["message_thread_id"], delivery_scope=r["delivery_scope"])
    row = ts.get_task(t["task_id"])
    check("4 persiste origin/notify/scope/bot/channel",
          row["origin_chat_id"] == MICHEL and row["notify_chat_id"] == MICHEL
          and row["delivery_scope"] == "origin" and row["bot_id"] == "8940907680"
          and row["origin_channel"] == "telegram")

    # 5 — monitor delivers to notify_chat_id (origin), NOT the owner
    sent = {}

    def stub_ok(chat, text, only_authorized=True):
        sent["chat"] = str(chat)
        return {"ok": True, "message_id": 777, "chat_masked": notify.mask_chat(chat),
                "delivered_chat_id": str(chat), "error": None}
    notify.send_message = stub_ok
    t2, _ = ts.create_task("garra", "flash", "engineering", "entrega na origem",
                           notify_chat_id=MICHEL, origin_chat_id=MICHEL, delivery_scope="origin",
                           monitor=True)
    ts.mark_accepted(t2["task_id"], 1); ts.mark_running(t2["task_id"]); ts.mark_succeeded(t2["task_id"], "ok")
    ts.set_monitor(t2["task_id"], active=True, last_notified_status=None)
    monitor.scan_once(verbose=False)
    ld = ts.last_delivered(t2["task_id"])
    check("5 monitor entrega para a ORIGEM 7978617919 (não owner)",
          sent.get("chat") == MICHEL and ld is not None and str(ld["message_id"]) == "777")

    # 6 — chat_mismatch -> delivery_failed/chat_mismatch, never delivered
    def stub_wrong(chat, text, only_authorized=True):
        return {"ok": True, "message_id": 888, "chat_masked": notify.mask_chat(chat),
                "delivered_chat_id": OWNER, "error": None}   # Telegram landed elsewhere
    notify.send_message = stub_wrong
    t3, _ = ts.create_task("garra", "flash", "engineering", "teste mismatch",
                           notify_chat_id=MICHEL, origin_chat_id=MICHEL, delivery_scope="origin",
                           monitor=True)
    ts.mark_accepted(t3["task_id"], 1); ts.mark_running(t3["task_id"]); ts.mark_succeeded(t3["task_id"], "ok")
    ts.set_monitor(t3["task_id"], active=True, last_notified_status=None)
    monitor.scan_once(verbose=False)
    notifs = ts.get_notifications(t3["task_id"])
    states = {n["status"] for n in notifs}
    errs = {n["error"] for n in notifs}
    check("6 chat_mismatch -> delivery_failed (não delivered)",
          "delivery_failed" in states and "delivered" not in states
          and "chat_mismatch" in errs and ts.last_delivered(t3["task_id"]) is None)

    print()
    if _fail:
        print(f"RESULT: {len(_fail)} FAIL -> {_fail}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.unlink(_tmp.name + suf)
            except OSError:
                pass
    sys.exit(rc)
