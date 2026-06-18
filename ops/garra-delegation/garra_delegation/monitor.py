#!/usr/bin/env python3
"""monitor — recurring heartbeat for delegated tasks (Garra's "10-minute monitor").

Runs one scan per invocation (driven by a systemd timer every 10 min). For each
task with monitor_active=1 it consults the REAL status and notifies the owner's
Telegram ONLY on a meaningful change:
  - task just started running  -> one "em execução" progress note
  - task near its timeout       -> one warning
  - task succeeded/failed/timed_out -> final delivery, then deactivate monitor.

It also detects dead workers (pid gone + stale heartbeat) and marks the task
timed_out instead of leaving it "running" forever. No state is ever invented:
every notification corresponds to a stored transition.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts   # noqa: E402
from garra_delegation import notify             # noqa: E402

STALE_HEARTBEAT_SECS = 90       # worker considered dead if no hb for this long & pid gone
NEAR_TIMEOUT_SECS = 120         # warn when this close to timeout


def _age(iso):
    if not iso:
        return 1e9
    try:
        t = datetime.fromisoformat(iso)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return 1e9


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _notify(task, text, kind="final"):
    """Deliver one Telegram notification with full delivery-state tracking.

    response_generated → delivery_pending → (delivered | delivery_failed), each
    persisted in the notifications ledger. Never reports delivered without a
    real message_id; retries are bounded by ts.MAX_DELIVERY_ATTEMPTS per kind.
    Returns {delivered, exhausted, message_id, error}.
    """
    tid = task["task_id"]
    chat = task["notify_chat_id"] or notify.owner_chat()
    masked = notify.mask_chat(chat)
    attempts = ts.delivery_attempts(tid, kind=kind)
    if attempts >= ts.MAX_DELIVERY_ATTEMPTS:
        return {"delivered": False, "exhausted": True, "message_id": None,
                "error": "max delivery attempts"}
    attempt = attempts + 1
    ts.record_notification(tid, "delivery_pending", chat_masked=masked,
                           attempt=attempt, kind=kind)
    res = notify.send_message(chat, text)
    if res.get("ok") and res.get("message_id") is not None:
        ts.record_notification(tid, "delivered", chat_masked=masked,
                               message_id=res["message_id"], attempt=attempt, kind=kind)
        return {"delivered": True, "exhausted": False, "message_id": res["message_id"]}
    ts.record_notification(tid, "delivery_failed", chat_masked=masked,
                           attempt=attempt, error=res.get("error"), kind=kind)
    return {"delivered": False, "exhausted": attempt >= ts.MAX_DELIVERY_ATTEMPTS,
            "message_id": None, "error": res.get("error")}


def scan_once(verbose=True):
    monitored = ts.list_monitored()
    actions = []
    for t in monitored:
        tid = t["task_id"]
        status = t["status"]

        # 1) dead-worker detection -> timed_out
        if status in ("accepted", "running") and not _pid_alive(t["worker_pid"]) \
                and _age(t["last_heartbeat_at"]) > STALE_HEARTBEAT_SECS:
            ts.mark_timed_out(tid, "worker desapareceu (pid morto, heartbeat parado)")
            t = ts.get_task(tid); status = t["status"]

        # 2) terminal -> final delivery; deactivate ONLY when delivery is
        #    confirmed (real message_id) or retries are exhausted. A failed send
        #    keeps the monitor active so the next scan retries (bounded).
        if status in ts.TERMINAL:
            if status == "succeeded":
                msg = (f"✅ Tarefa {tid} ({t['assigned_agent']}) CONCLUÍDA.\n\n"
                       f"{(t['result'] or '')[:1500]}")
            elif status == "cancelled":
                msg = f"🚫 Tarefa {tid} ({t['assigned_agent']}) cancelada."
            else:
                msg = (f"❌ Tarefa {tid} ({t['assigned_agent']}) {status}.\n"
                       f"Erro: {(t['error'] or '')[:400]}")
            d = _notify(t, msg, kind="final")
            if d["delivered"] or d["exhausted"]:
                ts.set_monitor(tid, active=False, last_notified_status=status)
            actions.append((tid, f"final:{status}:{'delivered' if d['delivered'] else 'pending'}"))
            continue

        # 3) running but not yet announced -> one progress note (on real delivery)
        if status in ("running", "accepted") and t["last_notified_status"] != "running":
            d = _notify(t, f"🛠️ Tarefa {tid} ({t['assigned_agent']}) em execução. "
                           f"Acompanhando; aviso quando concluir.", kind="progress")
            if d["delivered"] or d["exhausted"]:
                ts.set_monitor(tid, active=True, last_notified_status="running")
            actions.append((tid, f"progress:{'delivered' if d['delivered'] else 'pending'}"))
            continue

        # 4) near timeout warning (once, on real delivery)
        if status == "running" and _age(t["timeout_at"]) > -NEAR_TIMEOUT_SECS \
                and t["last_notified_status"] != "near_timeout":
            d = _notify(t, f"⏳ Tarefa {tid} ({t['assigned_agent']}) perto do timeout.",
                        kind="near_timeout")
            if d["delivered"] or d["exhausted"]:
                ts.set_monitor(tid, active=True, last_notified_status="near_timeout")
            actions.append((tid, f"near_timeout:{'delivered' if d['delivered'] else 'pending'}"))
            continue

        actions.append((tid, f"noop:{status}"))

    if verbose:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[monitor {stamp}] monitored={len(monitored)} actions={actions}")
    return {"monitored": len(monitored), "actions": actions}


if __name__ == "__main__":
    scan_once()
