#!/usr/bin/env python3
"""capabilities — real capability catalog + preflight + routing for Garra.

The catalog is validated against LIVE health checks (not static text), so before
delegating, Garra can verify: does the agent exist, is it available, does it have
the capability, and can it reach the resource. Enforces routing rules such as
"Alex has no Gmail — never send an e-mail search to Alex".
"""
import os
import shutil
import subprocess
import sys
import urllib.request

sys.path.insert(0, "/home/connect-car/.config/garraia")

CLAUDE_BIN = "/home/connect-car/.local/bin/claude"
HERMES_PY = "/home/connect-car/.hermes/hermes-agent/venv/bin/python"
N8N_HEALTH = "http://127.0.0.1:5678/healthz"

# Static declaration of what each actor CAN do. Availability is probed live.
CATALOG = {
    "garra": {
        "kind": "orchestrator",
        "capabilities": ["gmail.search", "gmail.read", "gmail.send", "gmail.reply",
                         "gmail.draft", "calendar.crud", "telegram.send",
                         "filesystem.read", "delegation"],
    },
    "flash": {
        "kind": "engineer (Claude Code)",
        "capabilities": ["code", "infra", "debugging", "n8n", "shell", "filesystem.write"],
        "agent": "flash",
    },
    "alex": {
        "kind": "specialist (Hermes/deepseek)",
        "capabilities": ["planning", "analysis", "coordination", "writing", "filesystem.write"],
        "agent": "alex",
        "lacks": ["gmail.search", "gmail.read", "gmail.send", "calendar.crud", "telegram.send"],
    },
}


def _http_ok(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def check_n8n():
    return {"name": "n8n", "ok": _http_ok(N8N_HEALTH), "endpoint": N8N_HEALTH}


def check_gmail():
    try:
        import gmail_lib
        ev = gmail_lib.whoami()
        return {"name": "gmail", "ok": ev["status"] == "succeeded",
                "account": ev.get("account"), "detail": ev.get("error")}
    except Exception as e:
        return {"name": "gmail", "ok": False, "detail": str(e)}


def check_calendar():
    try:
        import gcal_lib
        ev = gcal_lib.whoami()
        return {"name": "calendar", "ok": ev["status"] == "succeeded",
                "account": ev.get("account"), "detail": ev.get("error")}
    except Exception as e:
        return {"name": "calendar", "ok": False, "detail": str(e)}


def check_telegram():
    try:
        import garra_delegation.notify as notify
        me = notify.get_me()
        return {"name": "telegram", "ok": me.get("ok", False),
                "bot": me.get("result", {}).get("username")}
    except Exception as e:
        return {"name": "telegram", "ok": False, "detail": str(e)}


def check_flash():
    ok = os.path.exists(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)
    ver = None
    if ok:
        try:
            ver = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True,
                                 timeout=15).stdout.strip()[:40]
        except Exception:
            pass
    return {"name": "flash", "ok": ok, "agent": "flash", "version": ver, "bin": CLAUDE_BIN}


def check_alex():
    ok = os.path.exists(HERMES_PY)
    return {"name": "alex", "ok": ok, "agent": "alex", "python": HERMES_PY}


def health():
    """Live health of every component."""
    return {c["name"]: c for c in [
        check_n8n(), check_gmail(), check_calendar(), check_telegram(),
        check_flash(), check_alex()]}


def agent_available(agent):
    if agent == "flash":
        return check_flash()["ok"]
    if agent == "alex":
        return check_alex()["ok"]
    return False


def preflight(agent, capability=None, message=""):
    """Validate a delegation BEFORE it runs.

    Returns {"ok": bool, "reason": str, "suggestion": str}.
    Enforces: agent exists & available; agent actually has the capability;
    e-mail/calendar/telegram intents are NOT delegated to Alex (Garra does them).
    """
    if agent not in ("flash", "alex"):
        return {"ok": False, "reason": f"agente desconhecido '{agent}'",
                "suggestion": "use 'flash' (engenharia) ou 'alex' (análise)"}
    if not agent_available(agent):
        return {"ok": False, "reason": f"agente '{agent}' indisponível (binário/ambiente ausente)",
                "suggestion": "verifique a instalação"}
    low = (message or "").lower()
    email_intent = any(w in low for w in ("e-mail", "email", "gmail", "caixa de entrada",
                                          "inbox", "mensagem da", "remetente", "creativedge"))
    cal_intent = any(w in low for w in ("calendário", "calendar", "agenda", "evento", "reunião"))
    if agent == "alex" and (email_intent or capability in CATALOG["alex"].get("lacks", [])):
        return {"ok": False,
                "reason": "Alex NÃO tem ferramenta de Gmail/Calendar/Telegram",
                "suggestion": "use as ferramentas próprias da Garra (email__gmail_search/gmail_read) "
                              "para e-mail; delegue ao Alex apenas análise/planejamento/escrita"}
    if agent == "alex" and cal_intent:
        return {"ok": False, "reason": "Alex não acessa o Google Calendar",
                "suggestion": "use as ferramentas de calendário da Garra"}
    return {"ok": True, "reason": "ok", "suggestion": ""}


if __name__ == "__main__":
    import json
    print(json.dumps(health(), indent=2, ensure_ascii=False))
