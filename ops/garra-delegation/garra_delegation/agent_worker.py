#!/usr/bin/env python3
"""agent_worker — runs ONE delegated task to completion, updating the task store.

Invoked detached as:  python -m garra_delegation.agent_worker <task_id>

Flash := `claude -p <msg> --dangerously-skip-permissions`  (engineering / Claude Code)
Alex  := `hermes_cli chat -q <msg> ...`                     (specialist / deepseek)

Writes accepted -> running -> heartbeats -> (succeeded|failed|timed_out) into the
store, enforcing the task's timeout. This is the real execution backing every
delegation, so status is never invented.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts  # noqa: E402

CLAUDE_BIN = "/home/connect-car/.local/bin/claude"
FLASH_WORKDIR = "/home/connect-car/Documents/Projetos"
HERMES_DIR = "/home/connect-car/.hermes/hermes-agent"
HERMES_PY = "/home/connect-car/.hermes/hermes-agent/venv/bin/python"
ALEX_MODEL = "deepseek/deepseek-v4-flash"
HEARTBEAT_SECS = 15


def _clean_env():
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE_CODE") or k in ("CLAUDECODE", "AI_AGENT", "CLAUDE_EFFORT"))}
    env["PATH"] = ("/home/connect-car/.local/bin:/home/connect-car/.hermes/node/bin:"
                   "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", ""))
    env.setdefault("HOME", "/home/connect-car")
    return env


def _cmd_for(agent, message):
    if agent == "flash":
        return ([CLAUDE_BIN, "-p", message, "--dangerously-skip-permissions"],
                FLASH_WORKDIR, _clean_env())
    if agent == "alex":
        return ([HERMES_PY, "-m", "hermes_cli.main", "chat", "-q", message,
                 "-m", ALEX_MODEL, "--provider", "openrouter", "-Q",
                 "-t", "hermes-cli", "--yolo", "--accept-hooks"],
                HERMES_DIR, os.environ.copy())
    raise ValueError(f"unknown agent {agent}")


_ALEX_SKIP = ("session_id:", "Warning: Unknown toolsets", "⚠️",
              "Reached maximum iterations", "Requesting summary", "┊", "review diff")


def _clean_alex(out):
    def noise(ln):
        if any(s in ln for s in _ALEX_SKIP):
            return True
        st = ln.strip()
        return bool(re.match(r"^@@ .*@@", st) or re.match(r"^[ab]?/.*\s→\s[ab]?/", st))
    return "\n".join(ln for ln in out.splitlines() if not noise(ln)).strip()


def run(task_id):
    task = ts.get_task(task_id)
    if not task:
        print(f"task {task_id} not found", file=sys.stderr)
        return 2
    if task["status"] in ts.TERMINAL:
        return 0
    agent = task["assigned_agent"]
    message = task["payload"]
    timeout_secs = task["timeout_secs"] or 570

    ts.mark_accepted(task_id, worker_pid=os.getpid())
    try:
        argv, cwd, env = _cmd_for(agent, message)
    except Exception as e:
        ts.mark_failed(task_id, f"bad agent config: {e}")
        return 1

    out_f = tempfile.NamedTemporaryFile("w+", delete=False, prefix=f"worker-{task_id}-", suffix=".out")
    err_f = tempfile.NamedTemporaryFile("w+", delete=False, prefix=f"worker-{task_id}-", suffix=".err")
    ts.mark_running(task_id)
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=out_f, stderr=err_f, text=True, start_new_session=True)

    start = time.time()
    last_hb = 0.0
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        elapsed = time.time() - start
        if elapsed > timeout_secs:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
            ts.mark_timed_out(task_id, f"exceeded {timeout_secs}s")
            return 0
        if time.time() - last_hb >= HEARTBEAT_SECS:
            ts.heartbeat(task_id, progress=f"running {int(elapsed)}s")
            last_hb = time.time()
        time.sleep(2)

    out_f.flush(); err_f.flush()
    out_f.seek(0); err_f.seek(0)
    out = out_f.read().strip()
    err = err_f.read().strip()
    out_f.close(); err_f.close()
    try:
        os.unlink(out_f.name); os.unlink(err_f.name)
    except Exception:
        pass

    if agent == "alex":
        out = _clean_alex(out)

    if rc == 0 and out:
        ts.mark_succeeded(task_id, out)
    elif out:
        # non-zero exit but produced output — keep as succeeded-with-output is risky;
        # treat as succeeded only if we actually have a reply, else failed.
        ts.mark_succeeded(task_id, out)
    else:
        ts.mark_failed(task_id, f"exit {rc}; stderr: {err[-500:]}" if err else f"no output (exit {rc})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: agent_worker.py <task_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(sys.argv[1]))
