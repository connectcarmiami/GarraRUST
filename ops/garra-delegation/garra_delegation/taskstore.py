#!/usr/bin/env python3
"""Persistent task store for Garra delegations (SQLite, WAL).

States: queued -> accepted -> running -> (succeeded | failed | timed_out | cancelled)
        running may pass through waiting_external.

Every task carries the full required schema (task_id, parent_task_id, requested_by,
assigned_agent, capability, payload, status, created_at, accepted_at, started_at,
last_heartbeat_at, finished_at, result, error, retry_count, timeout_at,
correlation_id) plus dedup + monitor bookkeeping. An append-only task_events table
is the evidence/heartbeat trail.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("GARRA_TASKS_DB", "/home/connect-car/.garraia/data/tasks.db")

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}
ACTIVE = {"queued", "requested", "accepted", "claimed", "running", "waiting_external"}

# Canonical lifecycle required by the anti-hallucination contract. "queued" is
# kept as a back-compat alias for "requested" (older rows + current worker path).
ALL_STATES = TERMINAL | ACTIVE

# Legal forward transitions. A task may never leave a TERMINAL state, and a
# status may only move along these edges — so no caller (or fabricating model)
# can mark something "succeeded" out of nowhere.
_LEGAL = {
    "requested": {"accepted", "claimed", "running", "cancelled", "failed", "timed_out"},
    "queued": {"accepted", "claimed", "running", "cancelled", "failed", "timed_out"},
    "accepted": {"claimed", "running", "cancelled", "failed", "timed_out"},
    "claimed": {"running", "cancelled", "failed", "timed_out"},
    "running": {"running", "waiting_external", "succeeded", "failed", "timed_out", "cancelled"},
    "waiting_external": {"running", "succeeded", "failed", "timed_out", "cancelled"},
}

# Delivery lifecycle for a notification (heartbeat/monitor → Telegram).
DELIVERY_STATES = {"response_generated", "delivery_pending", "delivered", "delivery_failed"}
MAX_DELIVERY_ATTEMPTS = 3


class InvalidTransition(Exception):
    """Raised when a status change violates the task state machine."""


class MissingResult(Exception):
    """Raised when a task is marked succeeded without a persisted result."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    correlation_id TEXT,
    requested_by TEXT,
    assigned_agent TEXT,
    capability TEXT,
    payload TEXT,
    dedup_key TEXT,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 1,
    worker_pid INTEGER,
    notify_chat_id TEXT,
    monitor_active INTEGER DEFAULT 0,
    last_notified_status TEXT,
    progress TEXT,
    created_at TEXT,
    accepted_at TEXT,
    started_at TEXT,
    last_heartbeat_at TEXT,
    finished_at TEXT,
    timeout_at TEXT,
    timeout_secs INTEGER,
    provenance TEXT,
    check_count INTEGER DEFAULT 0,
    last_checked_at TEXT,
    last_checked_status TEXT,
    next_check_at TEXT,
    origin_chat_id TEXT,
    origin_channel TEXT,
    bot_id TEXT,
    message_thread_id TEXT,
    delivery_scope TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_dedup ON tasks(dedup_key);
CREATE INDEX IF NOT EXISTS idx_tasks_monitor ON tasks(monitor_active);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    ts TEXT,
    event TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);

-- Append-only delivery ledger: every attempt to push a Telegram notification
-- for a task, with the channel's real message_id (or the error). A task is only
-- ever reported "delivered" when there is a row here with a message_id.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    ts TEXT,
    status TEXT,            -- response_generated|delivery_pending|delivered|delivery_failed
    chat_masked TEXT,
    message_id TEXT,
    attempt INTEGER,
    error TEXT,
    kind TEXT               -- e.g. final/progress/near_timeout/heartbeat
);
CREATE INDEX IF NOT EXISTS idx_notif_task ON notifications(task_id);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
"""

# Columns added after the original schema shipped; applied to pre-existing DBs.
_MIGRATIONS = [
    ("tasks", "provenance", "TEXT"),
    ("tasks", "check_count", "INTEGER DEFAULT 0"),
    ("tasks", "last_checked_at", "TEXT"),
    ("tasks", "last_checked_status", "TEXT"),
    ("tasks", "next_check_at", "TEXT"),
    ("tasks", "origin_chat_id", "TEXT"),
    ("tasks", "origin_channel", "TEXT"),
    ("tasks", "bot_id", "TEXT"),
    ("tasks", "message_thread_id", "TEXT"),
    ("tasks", "delivery_scope", "TEXT"),
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = connect()
    with c:
        c.executescript(_SCHEMA)
        _apply_migrations(c)
    c.close()


def _apply_migrations(c):
    """Add columns introduced after the original schema, for existing DBs."""
    for table, col, decl in _MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # notifications table may be absent on DBs created before this change.
    c.executescript(
        "CREATE TABLE IF NOT EXISTS notifications ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts TEXT, status TEXT,"
        "chat_masked TEXT, message_id TEXT, attempt INTEGER, error TEXT, kind TEXT);"
        "CREATE INDEX IF NOT EXISTS idx_notif_task ON notifications(task_id);"
        "CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);"
    )


def normalize_intent(text):
    import unicodedata
    t = (text or "").lower().strip()
    # fold accents so "saúde"/"saude" and "versão"/"versao" dedupe alike
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t[:200]


def dedup_key(requested_by, agent, message, window_secs=900):
    """Key = requester + agent + normalized_intent + time_window bucket."""
    bucket = int(time.time() // window_secs)
    raw = f"{requested_by}|{agent}|{normalize_intent(message)}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def add_event(c, task_id, event, detail=""):
    c.execute("INSERT INTO task_events(task_id, ts, event, detail) VALUES(?,?,?,?)",
              (task_id, now_iso(), event, str(detail)[:2000]))


def find_active_by_dedup(c, key):
    row = c.execute(
        "SELECT * FROM tasks WHERE dedup_key=? AND status IN ('queued','accepted','running','waiting_external') "
        "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    return dict(row) if row else None


def create_task(requested_by, assigned_agent, capability, payload,
                timeout_secs=600, parent_task_id=None, correlation_id=None,
                notify_chat_id=None, max_retries=1, dedup_window=900, monitor=True,
                origin_chat_id=None, origin_channel=None, bot_id=None,
                message_thread_id=None, delivery_scope=None):
    """Create a task with dedup. Returns (task_dict, reused: bool).

    ``notify_chat_id`` is the chat the monitor/heartbeat will deliver to — it
    should be the conversation's real origin (``origin_chat_id``) and only fall
    back to the owner chat when there is no origin (``delivery_scope='fallback_owner'``).
    """
    init_db()
    c = connect()
    key = dedup_key(requested_by, assigned_agent, payload, dedup_window)
    reused = False
    try:
        with c:
            existing = find_active_by_dedup(c, key)
            if existing:
                add_event(c, existing["task_id"], "dedup_hit", f"reused by {requested_by}")
                return existing, True
            tid = "t-" + uuid.uuid4().hex[:12]
            cid = correlation_id or ("corr-" + uuid.uuid4().hex[:10])
            ts = now_iso()
            timeout_at = datetime.fromtimestamp(time.time() + timeout_secs, timezone.utc).isoformat(timespec="seconds")
            c.execute("""INSERT INTO tasks
                (task_id,parent_task_id,correlation_id,requested_by,assigned_agent,capability,
                 payload,dedup_key,status,retry_count,max_retries,notify_chat_id,monitor_active,
                 created_at,timeout_at,timeout_secs,
                 origin_chat_id,origin_channel,bot_id,message_thread_id,delivery_scope)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (tid, parent_task_id, cid, requested_by, assigned_agent, capability,
                       payload, key, "queued", 0, max_retries, notify_chat_id,
                       1 if monitor else 0, ts, timeout_at, timeout_secs,
                       origin_chat_id, origin_channel,
                       str(bot_id) if bot_id is not None else None,
                       str(message_thread_id) if message_thread_id is not None else None,
                       delivery_scope))
            add_event(c, tid, "created",
                      f"agent={assigned_agent} cap={capability} scope={delivery_scope} "
                      f"notify={notify_chat_id} origin={origin_chat_id}")
            row = dict(c.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone())
    finally:
        c.close()
    return row, reused


def _set(task_id, **fields):
    """Update non-status fields. Status changes MUST go through _transition."""
    if "status" in fields:
        raise ValueError("use _transition() to change status, not _set()")
    c = connect()
    with c:
        cols = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE tasks SET {cols} WHERE task_id=?", (*fields.values(), task_id))
    c.close()


def _transition(task_id, new_status, event=None, detail="", **fields):
    """Atomically move a task to ``new_status``, enforcing the state machine.

    Rejects: unknown task, leaving a TERMINAL state, and any edge not in _LEGAL.
    Records the matching task_event so every state change is auditable evidence.
    """
    if new_status not in ALL_STATES:
        raise InvalidTransition(f"unknown status {new_status!r}")
    c = connect()
    try:
        with c:
            row = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise InvalidTransition(f"task {task_id} not found")
            old = row["status"]
            if old in TERMINAL:
                raise InvalidTransition(f"{task_id} já terminal ({old}); não pode virar {new_status}")
            if new_status != old and new_status not in _LEGAL.get(old, set()):
                raise InvalidTransition(f"transição ilegal {old} -> {new_status}")
            fields["status"] = new_status
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE tasks SET {cols} WHERE task_id=?", (*fields.values(), task_id))
            add_event(c, task_id, event or new_status, detail)
    finally:
        c.close()


def mark_accepted(task_id, worker_pid=None):
    _transition(task_id, "accepted", detail=f"pid={worker_pid}",
                accepted_at=now_iso(), worker_pid=worker_pid)


def mark_claimed(task_id, worker_pid=None):
    _transition(task_id, "claimed", detail=f"pid={worker_pid}", worker_pid=worker_pid)


def mark_running(task_id):
    _transition(task_id, "running", started_at=now_iso(), last_heartbeat_at=now_iso())


def heartbeat(task_id, progress=None):
    fields = {"last_heartbeat_at": now_iso()}
    if progress is not None:
        fields["progress"] = str(progress)[:500]
    _set(task_id, **fields)
    c = connect()
    with c:
        add_event(c, task_id, "heartbeat", progress or "")
    c.close()


def mark_succeeded(task_id, result, provenance=None):
    """succeeded REQUIRES a persisted, non-empty result (anti-hallucination)."""
    text = "" if result is None else str(result)
    if not text.strip():
        raise MissingResult(f"recusando marcar {task_id} succeeded sem resultado real")
    prov = provenance or json.dumps({"source": "tool_result", "chars": len(text)},
                                    ensure_ascii=False)
    _transition(task_id, "succeeded", detail=f"{len(text)} chars",
                result=text, provenance=prov, finished_at=now_iso(),
                last_heartbeat_at=now_iso())


def mark_failed(task_id, error):
    _transition(task_id, "failed", detail=str(error)[:500],
                error=str(error)[:4000], finished_at=now_iso())


def mark_timed_out(task_id, detail=""):
    _transition(task_id, "timed_out", detail=detail,
                error=f"timeout: {detail}"[:4000], finished_at=now_iso())


def mark_cancelled(task_id, detail=""):
    _transition(task_id, "cancelled", detail=detail, finished_at=now_iso())


def set_monitor(task_id, active, last_notified_status=None):
    fields = {"monitor_active": 1 if active else 0}
    if last_notified_status is not None:
        fields["last_notified_status"] = last_notified_status
    _set(task_id, **fields)


def get_task(task_id):
    c = connect()
    row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_events(task_id, limit=50):
    c = connect()
    rows = c.execute("SELECT ts,event,detail FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                     (task_id, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_tasks(status=None, agent=None, active_only=False, limit=20):
    c = connect()
    q = "SELECT * FROM tasks"
    conds, args = [], []
    if status:
        conds.append("status=?"); args.append(status)
    if agent:
        conds.append("assigned_agent=?"); args.append(agent)
    if active_only:
        conds.append("status IN ('queued','accepted','running','waiting_external')")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_monitored():
    c = connect()
    rows = c.execute("SELECT * FROM tasks WHERE monitor_active=1").fetchall()
    c.close()
    return [dict(r) for r in rows]


def evidence(task):
    """Compact, non-sensitive evidence dict for a task."""
    if not task:
        return {"status": "unknown"}
    return {
        "task_id": task["task_id"], "agent": task["assigned_agent"],
        "capability": task["capability"], "status": task["status"],
        "correlation_id": task["correlation_id"],
        "created_at": task["created_at"], "accepted_at": task["accepted_at"],
        "started_at": task["started_at"], "last_heartbeat_at": task["last_heartbeat_at"],
        "finished_at": task["finished_at"], "timeout_at": task["timeout_at"],
        "retry_count": task["retry_count"], "worker_pid": task["worker_pid"],
        "has_result": bool(task["result"]), "error": task["error"],
        "provenance": task["provenance"] if "provenance" in task.keys() else None,
        "notify_chat_id": task["notify_chat_id"],
        "origin_chat_id": task["origin_chat_id"] if "origin_chat_id" in task.keys() else None,
        "delivery_scope": task["delivery_scope"] if "delivery_scope" in task.keys() else None,
    }


# ── Auditable, masked, secret-free metadata for check_task ────────────────────

def _mask_audit(chat_id, keep=7):
    """Keep the first `keep` chars + '***' (ex.: 7978617919 -> 7978617***)."""
    s = str(chat_id or "")
    if not s:
        return None
    return s if len(s) <= keep else s[:keep] + "***"


def mask_taskid(task_id):
    """Mask a NON-EXISTENT/UNVERIFIED id so it is shown but NOT harvestable by the
    output guard (anti-laundering). 't-aaaabbbbcccc' -> 't-aaaab***' (the hex run
    after 't-' is < 8 chars, so the guard scanner does not treat it as a real id).
    Real/existing ids are NEVER masked — only ids that failed verification."""
    s = str(task_id or "")
    if not s:
        return "(vazio)"
    return s if len(s) <= 7 else s[:7] + "***"


def audit_metadata(task_id):
    """Masked, secret-free audit view of a task (for delegation__check_task).

    Never exposes tokens. Chat ids are masked (first-7 + '***'). `chat_ok`/
    `message_id_present` come from the delivery ledger (real evidence), not
    inference. Returns verdict UNVERIFIED when the id does not exist.
    """
    t = get_task(task_id)
    if not t:
        # Masked, non-harvestable — never echo a non-existent id verbatim.
        return {"queried_masked": mask_taskid(task_id), "exists": False,
                "verdict": "UNVERIFIED", "reason": "id não existe no task store autorizado"}
    ld = last_delivered(task_id)                       # confirmed delivery (or None)
    keys = t.keys()
    origin = t["origin_chat_id"] if "origin_chat_id" in keys else None
    notify_c = t["notify_chat_id"]
    result = t["result"] or ""
    chat_ids_match = bool(origin and notify_c and str(origin) == str(notify_c))
    if ld is None:
        chat_ok = None                                 # sem entrega confirmada — não afirmar
    else:
        chat_ok = bool(notify_c and ld["chat_masked"]
                       and str(notify_c).endswith(str(ld["chat_masked"])[-4:]))
    return {
        "exists": True, "verdict": "PASS",
        "task_id": t["task_id"],                       # id real (vindo do store)
        "status": t["status"],
        "has_result": bool(result),
        "created_at": t["created_at"],
        "completed_at": t["finished_at"],
        "error": (t["error"] or None),
        "result_ref": (f"task:{t['task_id']}:result" if result else None),
        "result_summary": ((result[:160] + ("…" if len(result) > 160 else "")) if result else None),
        "origin_chat_id_masked": _mask_audit(origin),
        "notify_chat_id_masked": _mask_audit(notify_c),
        "chat_ids_match": chat_ids_match,
        "delivery_scope": t["delivery_scope"] if "delivery_scope" in keys else None,
        "message_id_present": bool(ld and ld["message_id"]),
        "message_id": (ld["message_id"] if ld else None),
        "chat_ok": chat_ok,
    }


# ── Evidence binding: an identifier is real ONLY if it is in this store ───────

def verify_identifier(task_id):
    """The authority that turns 'a model typed t-xxxx' into PASS/UNVERIFIED.

    Returns {"verified": bool, "verdict": "PASS"|"UNVERIFIED", ...evidence}.
    An identifier with no row here is UNVERIFIED — it cannot be cited as real.
    """
    t = get_task(task_id)
    if not t:
        # Do NOT echo the raw id — that would let an invented id be harvested by
        # the output guard and laundered into "verified". Show it masked.
        return {"verified": False, "verdict": "UNVERIFIED",
                "queried_masked": mask_taskid(task_id),
                "reason": "identificador não existe no task store autorizado"}
    ev = evidence(t)
    ev.update({"verified": True, "verdict": "PASS"})
    return ev


# ── Delivery ledger (heartbeat/monitor → Telegram) ───────────────────────────

def record_notification(task_id, status, chat_masked=None, message_id=None,
                        attempt=None, error=None, kind=None):
    """Append one delivery-ledger row. ``status`` ∈ DELIVERY_STATES."""
    if status not in DELIVERY_STATES:
        raise ValueError(f"estado de entrega inválido: {status!r}")
    c = connect()
    with c:
        c.execute(
            "INSERT INTO notifications(task_id,ts,status,chat_masked,message_id,attempt,error,kind) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, now_iso(), status, chat_masked,
             str(message_id) if message_id is not None else None,
             attempt, str(error)[:500] if error else None, kind))
        nid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        add_event(c, task_id, f"delivery.{status}",
                  f"msg_id={message_id} attempt={attempt} kind={kind}")
    c.close()
    return nid


def get_notifications(task_id, limit=20):
    c = connect()
    rows = c.execute(
        "SELECT ts,status,chat_masked,message_id,attempt,error,kind FROM notifications "
        "WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delivery_attempts(task_id, kind=None):
    c = connect()
    if kind:
        n = c.execute("SELECT COUNT(*) FROM notifications WHERE task_id=? AND kind=? "
                      "AND status IN ('delivery_pending','delivery_failed','delivered')",
                      (task_id, kind)).fetchone()[0]
    else:
        n = c.execute("SELECT COUNT(*) FROM notifications WHERE task_id=? "
                      "AND status IN ('delivery_pending','delivery_failed','delivered')",
                      (task_id,)).fetchone()[0]
    c.close()
    return n


def last_delivered(task_id, kind=None):
    """The most recent CONFIRMED delivery (status=delivered with a message_id)."""
    c = connect()
    q = ("SELECT ts,message_id,chat_masked,attempt,kind FROM notifications "
         "WHERE task_id=? AND status='delivered' AND message_id IS NOT NULL")
    args = [task_id]
    if kind:
        q += " AND kind=?"; args.append(kind)
    q += " ORDER BY id DESC LIMIT 1"
    row = c.execute(q, args).fetchone()
    c.close()
    return dict(row) if row else None


# ── Polling guard: at most one useful check; BLOCKED instead of looping ───────

_CHECK_BACKOFF = [10, 20, 40, 80, 160, 300]   # seconds; grows then caps at 300
MAX_CHECKS = 8


def _backoff_secs(n):
    return _CHECK_BACKOFF[min(n, len(_CHECK_BACKOFF) - 1)]


def register_check(task_id):
    """Throttle repeated status polls. Returns a decision dict; never loops.

    {allowed, reason, status, changed, check_count, next_check_at, verdict}
    - terminal tasks are always allowed (read the real result);
    - an identical poll before next_check_at with unchanged status → BLOCKED;
    - more than MAX_CHECKS non-terminal polls → BLOCKED (rely on the monitor).
    """
    t = get_task(task_id)
    if not t:
        return {"allowed": False, "verdict": "UNVERIFIED", "status": "unknown",
                "reason": f"tarefa {task_id} não existe", "task_id": task_id}
    status = t["status"]
    now = time.time()
    last_status = t["last_checked_status"] if "last_checked_status" in t.keys() else None
    count = (t["check_count"] or 0) if "check_count" in t.keys() else 0
    next_at = t["next_check_at"] if "next_check_at" in t.keys() else None
    changed = (status != last_status)

    if status in TERMINAL:
        # record the read but do not throttle terminal reads
        _set(task_id, check_count=count + 1, last_checked_at=now_iso(),
             last_checked_status=status)
        return {"allowed": True, "verdict": "PASS", "status": status, "changed": changed,
                "check_count": count + 1, "next_check_at": None, "task_id": task_id}

    # not terminal: throttle
    too_soon = False
    if next_at:
        try:
            na = datetime.fromisoformat(next_at).timestamp()
            too_soon = now < na
        except Exception:
            too_soon = False

    if not changed and too_soon:
        return {"allowed": False, "verdict": "BLOCKED", "status": status, "changed": False,
                "check_count": count, "next_check_at": next_at, "task_id": task_id,
                "reason": ("sem novidade desde a última consulta; aguarde — o monitor "
                           "avisa no Telegram quando mudar")}
    if count >= MAX_CHECKS:
        return {"allowed": False, "verdict": "BLOCKED", "status": status, "changed": changed,
                "check_count": count, "next_check_at": next_at, "task_id": task_id,
                "reason": (f"limite de {MAX_CHECKS} consultas atingido; pare de consultar e "
                           "confie no monitor recorrente")}

    new_next = datetime.fromtimestamp(now + _backoff_secs(count + 1), timezone.utc).isoformat(timespec="seconds")
    _set(task_id, check_count=count + 1, last_checked_at=now_iso(),
         last_checked_status=status, next_check_at=new_next)
    return {"allowed": True, "verdict": "PASS", "status": status, "changed": changed,
            "check_count": count + 1, "next_check_at": new_next, "task_id": task_id}


if __name__ == "__main__":
    import sys
    init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for t in list_tasks(limit=30):
            print(t["task_id"], t["status"], t["assigned_agent"], (t["payload"] or "")[:50])
    elif cmd == "get":
        print(json.dumps(get_task(sys.argv[2]), indent=2, ensure_ascii=False))
        print("EVENTS:")
        for e in get_events(sys.argv[2]):
            print(" ", e["ts"], e["event"], e["detail"][:80])
    elif cmd == "init":
        print("db initialized at", DB_PATH)
