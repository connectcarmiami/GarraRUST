#!/usr/bin/env python3
"""Acceptance: check_task audit metadata (masked, secret-free) + monitor never
infers timed_out from timeout_at. Hermetic (throwaway DB + stub Telegram)."""
import os
import sys
import tempfile
import time

_tmp = tempfile.NamedTemporaryFile(prefix="audit_mon_", suffix=".db", delete=False)
_tmp.close()
os.environ["GARRA_TASKS_DB"] = _tmp.name

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts, notify, monitor   # noqa: E402

MICHEL = "7978617919"
OWNER = "8890643175"
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

    # B1 — audit metadata of a real origin task: masked, no full id, all fields
    t, _ = ts.create_task("garra", "flash", "engineering", "audit real",
                          notify_chat_id=MICHEL, origin_chat_id=MICHEL,
                          delivery_scope="origin", monitor=True)
    tid = t["task_id"]
    ts.mark_accepted(tid, 1); ts.mark_running(tid); ts.mark_succeeded(tid, "resultado real do flash")
    a = ts.audit_metadata(tid)
    import json
    blob = json.dumps(a, ensure_ascii=False)
    check("B1 audit: id real + status + has_result + scope",
          a["task_id"] == tid and a["status"] == "succeeded" and a["has_result"]
          and a["delivery_scope"] == "origin")
    check("B1 audit: chats MASCARADOS (sem id completo)",
          a["origin_chat_id_masked"] == "7978617***" and a["notify_chat_id_masked"] == "7978617***"
          and MICHEL not in blob)
    check("B1 audit: chat_ids_match true", a["chat_ids_match"] is True)
    check("B1 audit: campos de auditoria presentes",
          all(k in a for k in ("created_at", "completed_at", "error", "result_ref",
                               "result_summary", "message_id_present", "chat_ok")))

    # B2 — invented id -> UNVERIFIED, exists False
    a2 = ts.audit_metadata("t-falso123")
    check("B2 audit id inventado -> UNVERIFIED", a2["verdict"] == "UNVERIFIED" and a2["exists"] is False)

    # B3 — delivered task: message_id_present + chat_ok from ledger (not inference)
    def stub_ok(chat, text, only_authorized=True):
        return {"ok": True, "message_id": 999, "chat_masked": notify.mask_chat(chat),
                "delivered_chat_id": str(chat), "error": None}
    notify.send_message = stub_ok
    t3, _ = ts.create_task("garra", "monitor", "heartbeat", "deliver", notify_chat_id=MICHEL,
                           origin_chat_id=MICHEL, delivery_scope="origin", monitor=True)
    ts.mark_accepted(t3["task_id"], 1); ts.mark_running(t3["task_id"]); ts.mark_succeeded(t3["task_id"], "ok")
    ts.set_monitor(t3["task_id"], active=True, last_notified_status=None)
    monitor.scan_once(verbose=False)
    a3 = ts.audit_metadata(t3["task_id"])
    check("B3 audit: message_id_present + chat_ok from ledger",
          a3["message_id_present"] is True and a3["message_id"] == "999" and a3["chat_ok"] is True)

    # C1 — running task PAST its timeout_at but worker ALIVE + fresh heartbeat:
    #      monitor must NOT mark timed_out (no timeout_at inference).
    import os as _os
    live_pid = _os.getpid()  # this process is alive
    t4, _ = ts.create_task("garra", "flash", "engineering", "long running",
                           timeout_secs=1, notify_chat_id=MICHEL, monitor=True)  # timeout_at ~now
    ts.mark_accepted(t4["task_id"], live_pid); ts.mark_running(t4["task_id"])
    time.sleep(2)  # now PAST timeout_at, but pid alive + heartbeat fresh
    monitor.scan_once(verbose=False)
    check("C1 monitor NÃO inventa timed_out (worker vivo, passou timeout_at)",
          ts.get_task(t4["task_id"])["status"] == "running")

    # C2 — dead worker (pid impossible) + stale heartbeat: monitor MAY timed_out
    #      (real signal). Force a stale heartbeat.
    t5, _ = ts.create_task("garra", "flash", "engineering", "dead worker",
                           timeout_secs=600, notify_chat_id=MICHEL, monitor=True)
    ts.mark_accepted(t5["task_id"], 999999); ts.mark_running(t5["task_id"])
    # backdate heartbeat to be stale
    cc = ts.connect()
    with cc:
        cc.execute("UPDATE tasks SET last_heartbeat_at=? WHERE task_id=?",
                   ("2020-01-01T00:00:00+00:00", t5["task_id"]))
    cc.close()
    monitor.scan_once(verbose=False)
    check("C2 monitor timed_out por sinal real (worker morto + heartbeat parado)",
          ts.get_task(t5["task_id"])["status"] == "timed_out")

    # C3 — race-safe: a task that goes terminal between snapshot and transition
    #      must not crash the scan (InvalidTransition swallowed). Simulate by a
    #      task already succeeded but still flagged monitor_active with dead pid.
    t6, _ = ts.create_task("garra", "flash", "engineering", "raced", notify_chat_id=MICHEL, monitor=True)
    ts.mark_accepted(t6["task_id"], 999998); ts.mark_running(t6["task_id"]); ts.mark_succeeded(t6["task_id"], "done")
    ts.set_monitor(t6["task_id"], active=True, last_notified_status=None)
    try:
        monitor.scan_once(verbose=False)  # must not raise
        check("C3 scan race-safe (não quebra com task terminal)", True)
    except Exception as e:
        check("C3 scan race-safe", False, str(e)[:60])

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
