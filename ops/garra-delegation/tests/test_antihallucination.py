#!/usr/bin/env python3
"""Anti-hallucination acceptance suite for Garra's delegation/evidence layer.

Runs against a THROWAWAY tasks DB (GARRA_TASKS_DB) and a STUBBED Telegram
adapter, so it is hermetic and safe to run anywhere. Each check prints
PASS/FAIL; the process exits non-zero if any check fails.

Covered (matches the operational contract):
  1  invented identifier is UNVERIFIED (cannot be cited as real)
  2  a task "narrated" without a real create stays UNVERIFIED
  3  a real, persisted result CAN be cited (PASS)
  4  repeated identical check_task is BLOCKED (no polling loop)
  5  poll backoff grows; equivalent task is de-duplicated
  6  succeeded WITHOUT a result is rejected
  7  illegal state transitions are rejected; terminal is sticky
  8  Telegram adapter error / absent → delivery_failed, NOT delivered
  9  delivery retried then exhausted (bounded), monitor stays active until then
 10  real message_id is persisted on confirmed delivery
 11  persistence survives a fresh DB connection ("restart")
"""
import os
import sys
import tempfile

_TMP = tempfile.NamedTemporaryFile(prefix="garra_test_", suffix=".db", delete=False)
_TMP.close()
os.environ["GARRA_TASKS_DB"] = _TMP.name

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts        # noqa: E402
from garra_delegation import notify                  # noqa: E402
from garra_delegation import monitor                 # noqa: E402

_failures = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def _new_task(agent="flash", payload="trabalho real", monitor_flag=False):
    t, _ = ts.create_task("garra", agent, "engineering", payload,
                          timeout_secs=600, notify_chat_id="8890643175",
                          monitor=monitor_flag)
    return t["task_id"]


def main():
    ts.init_db()

    # 1 — invented identifier (the exact ones Garra fabricated) is UNVERIFIED
    for fake in ("t-7f4e2c9a1b8d", "t-8a3f5d7e2c9b"):
        v = ts.verify_identifier(fake)
        check(f"1 invented {fake} -> UNVERIFIED", v["verdict"] == "UNVERIFIED")

    # 2 — narrated-but-never-created task is UNVERIFIED + register_check refuses
    v = ts.verify_identifier("t-deadbeef0000")
    d = ts.register_check("t-deadbeef0000")
    check("2 narrated task UNVERIFIED", v["verdict"] == "UNVERIFIED" and not d["allowed"])

    # 3 — a real persisted result CAN be cited
    tid = _new_task()
    ts.mark_accepted(tid, 1234)
    ts.mark_running(tid)
    ts.mark_succeeded(tid, "resposta real e persistida do Flash")
    v = ts.verify_identifier(tid)
    check("3 real result citable (PASS)", v["verdict"] == "PASS" and v["has_result"])

    # 4 — repeated identical check is BLOCKED
    tid4 = _new_task(payload="poll-target")
    ts.mark_accepted(tid4, 1)
    ts.mark_running(tid4)
    d1 = ts.register_check(tid4)
    d2 = ts.register_check(tid4)
    check("4 repeated check BLOCKED", d1["allowed"] and not d2["allowed"]
          and d2["verdict"] == "BLOCKED")

    # 5 — backoff grows; equivalent task de-duplicated
    grew = (d2.get("next_check_at") is not None)
    a, ra = ts.create_task("garra", "flash", "engineering", "tarefa idêntica para dedup")
    b, rb = ts.create_task("garra", "flash", "engineering", "tarefa idêntica para dedup")
    check("5 dedup reuses active task", (not ra) and rb and a["task_id"] == b["task_id"])

    # 6 — succeeded without result rejected
    tid6 = _new_task(payload="will-have-no-result")
    ts.mark_accepted(tid6, 1)
    ts.mark_running(tid6)
    rejected = False
    try:
        ts.mark_succeeded(tid6, "   ")
    except ts.MissingResult:
        rejected = True
    check("6 succeeded-without-result rejected", rejected
          and ts.get_task(tid6)["status"] == "running")

    # 7 — illegal transition rejected / terminal sticky
    tid7 = _new_task(payload="state-machine")
    ts.mark_accepted(tid7, 1)
    ts.mark_running(tid7)
    ts.mark_succeeded(tid7, "ok")
    sticky = False
    try:
        ts.mark_running(tid7)
    except ts.InvalidTransition:
        sticky = True
    check("7 terminal is sticky", sticky)

    # ── Delivery tests: stub the Telegram adapter ───────────────────────────
    orig_send = notify.send_message
    orig_owner = notify.owner_chat

    notify.owner_chat = lambda: "8890643175"

    # 8 — adapter error → delivery_failed, never delivered
    notify.send_message = lambda chat, text, only_authorized=True: {
        "ok": False, "message_id": None, "chat_masked": notify.mask_chat(chat),
        "error": "Forbidden: bot blocked by user"}
    tid8 = _new_task(payload="deliver-fail", monitor_flag=True)
    ts.mark_accepted(tid8, 1)
    ts.mark_running(tid8)
    ts.mark_succeeded(tid8, "resultado pronto")
    monitor.scan_once(verbose=False)
    notifs = ts.get_notifications(tid8)
    states = {n["status"] for n in notifs}
    check("8 adapter error -> delivery_failed not delivered",
          "delivery_failed" in states and "delivered" not in states
          and ts.last_delivered(tid8) is None)
    still_active = ts.get_task(tid8)["monitor_active"] == 1
    check("9a monitor stays active after failed delivery (will retry)", still_active)

    # 9 — keep failing until exhausted → monitor deactivates
    for _ in range(ts.MAX_DELIVERY_ATTEMPTS + 1):
        monitor.scan_once(verbose=False)
    exhausted_inactive = ts.get_task(tid8)["monitor_active"] == 0
    attempts = ts.delivery_attempts(tid8, kind="final")
    check("9b retry bounded then monitor off",
          exhausted_inactive and attempts >= ts.MAX_DELIVERY_ATTEMPTS,
          f"attempts={attempts}")

    # 10 — confirmed delivery persists the real message_id
    notify.send_message = lambda chat, text, only_authorized=True: {
        "ok": True, "message_id": 424242, "chat_masked": notify.mask_chat(chat),
        "error": None}
    tid10 = _new_task(payload="deliver-ok", monitor_flag=True)
    ts.mark_accepted(tid10, 1)
    ts.mark_running(tid10)
    ts.mark_succeeded(tid10, "tudo certo")
    monitor.scan_once(verbose=False)
    ld = ts.last_delivered(tid10)
    check("10 message_id persisted on delivery",
          ld is not None and str(ld["message_id"]) == "424242"
          and ts.get_task(tid10)["monitor_active"] == 0)

    notify.send_message = orig_send
    notify.owner_chat = orig_owner

    # 11 — persistence across a fresh connection ("restart")
    reopened = ts.get_task(tid10)
    check("11 persists across reopen", reopened is not None
          and reopened["status"] == "succeeded"
          and ts.last_delivered(tid10)["message_id"] is not None)

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAIL -> {_failures}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.unlink(_TMP.name + suf)
            except OSError:
                pass
    sys.exit(rc)
