# Garra delegation layer — anti-hallucination snapshot (2026-06-18)

This directory is a **version-controlled snapshot** of the Python delegation /
evidence layer that runs in production at `~/.config/garraia/` (executed by the
Hermes venv, registered as the `delegation` MCP server and driven by the
`garra-monitor.timer`). It is committed here so the anti-hallucination fix is
auditable in one place alongside the Rust runtime guard; it is **not** built into
the `garra` binary and is deployed by copying these files to
`~/.config/garraia/`.

## Contents
- `delegation_mcp.py` — MCP server exposing `delegation__*` tools (ask_flash,
  ask_alex, check_task, verify_task, schedule_heartbeat, get_task_result,
  list_tasks, cancel_task, agent_capabilities).
- `garra_delegation/` — package:
  - `taskstore.py` — SQLite task store: state machine, `succeeded` requires a
    persisted result, `verify_identifier` (PASS/UNVERIFIED), notification
    delivery ledger (`message_id` persisted), polling guard (`register_check` →
    BLOCKED instead of looping).
  - `notify.py` — Telegram delivery returning structured `{ok, message_id,
    chat_masked, error}`; only reports delivered with a real `message_id`.
  - `monitor.py` — recurring monitor; separates delivery states, persists
    `message_id`, bounded retry, only deactivates on confirmed delivery.
  - `agent_worker.py` — detached worker that runs one delegated task.
  - `capabilities.py` — live capability catalog + preflight routing.
- `tests/test_antihallucination.py` — hermetic acceptance suite (throwaway DB +
  stubbed Telegram adapter): invented id → UNVERIFIED, repeated check → BLOCKED,
  succeeded-without-result rejected, adapter error → delivery_failed, bounded
  retry, `message_id` persisted, restart persistence. Run:
  `PYTHONPATH=. <hermes-venv>/python tests/test_antihallucination.py`.

## Why this exists
The runtime guard lives in `crates/garraia-agents/src/output_guard.rs` (binds
reply identifiers to real tool evidence). This Python layer is the persistence /
delegation side: it is the authority that turns "the model typed `t-xxxx`" into
PASS (exists in the authorized store) or UNVERIFIED, and the source of the
evidence the guard checks. See `docs/anti-hallucination-guard-2026-06-18.md`.

> Snapshot reference, not the source of truth for the live process. If the
> deployed files at `~/.config/garraia/` change, re-sync this snapshot.
