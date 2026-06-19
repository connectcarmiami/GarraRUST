#!/usr/bin/env python3
"""
Delegation MCP server — Garra's repaired, evidence-backed bridge to Flash & Alex.

Replaces the old fire-and-forget flash_mcp/supergarra_mcp. Every delegation now:
  - is preflighted against the real capability catalog (e.g. NEVER routes a Gmail
    task to Alex),
  - is persisted in the task store (task_id, status, timestamps, heartbeat,
    timeout, retries, correlation_id, dedup),
  - runs in a DETACHED background worker (so long tasks don't block Garra),
  - is tracked by the recurring monitor (heartbeat) until it finishes,
  - returns STRUCTURED EVIDENCE so Garra can only report real status.

Garra sees the tools as `delegation__<name>`:
  ask_flash, ask_alex, check_task, get_task_result, list_tasks, cancel_task,
  agent_capabilities.
"""
import json
import os
import signal
import subprocess
import sys
import time

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts          # noqa: E402
from garra_delegation import capabilities as cap        # noqa: E402
from garra_delegation import notify                     # noqa: E402

HERMES_PY = "/home/connect-car/.hermes/hermes-agent/venv/bin/python"
PKG_DIR = "/home/connect-car/.config/garraia"
WORKER_LOG_DIR = "/home/connect-car/garra-fix/logs"
FAST_WAIT_SECS = 75          # inline wait for quick tasks before handing off to monitor
FLASH_TIMEOUT = 570
ALEX_TIMEOUT = 570

mcp = FastMCP("delegation")


def _spawn_worker(task_id):
    os.makedirs(WORKER_LOG_DIR, exist_ok=True)
    logf = open(os.path.join(WORKER_LOG_DIR, f"worker-{task_id}.log"), "a")
    env = os.environ.copy()
    env["PYTHONPATH"] = PKG_DIR + ":" + env.get("PYTHONPATH", "")
    p = subprocess.Popen(
        [HERMES_PY, "-m", "garra_delegation.agent_worker", task_id],
        cwd=PKG_DIR, env=env, stdin=subprocess.DEVNULL,
        stdout=logf, stderr=logf, start_new_session=True)
    return p.pid


def _wait_terminal(task_id, secs):
    deadline = time.time() + secs
    while time.time() < deadline:
        t = ts.get_task(task_id)
        if t and t["status"] in ts.TERMINAL:
            return t
        time.sleep(2)
    return ts.get_task(task_id)


def _ev(task):
    return "EVIDÊNCIA: " + json.dumps(ts.evidence(task), ensure_ascii=False)


def _resolve_routing(origin_chat_id):
    """Decide where notifications for this task go.

    The conversation's real origin (when authenticated/authorized) ALWAYS wins;
    the owner chat is ONLY a fallback when there is no usable origin
    (delivery_scope='fallback_owner'). This is the fix for notifications landing
    in the hardcoded owner chat instead of the chat the request came from.
    """
    authd = notify.authorized_chats()
    oc = str(origin_chat_id).strip() if origin_chat_id else ""
    bid = notify.bot_id()
    if oc and oc in authd:
        return {"origin_chat_id": oc, "notify_chat_id": oc, "origin_channel": "telegram",
                "bot_id": bid, "message_thread_id": None, "delivery_scope": "origin"}
    # no origin (or an origin we won't deliver to) -> owner, clearly flagged
    return {"origin_chat_id": oc or None, "notify_chat_id": notify.owner_chat(),
            "origin_channel": "telegram" if oc else None, "bot_id": bid,
            "message_thread_id": None, "delivery_scope": "fallback_owner"}


def _delegate(agent, message, capability, origin_chat_id=None, origin_session_id=None):
    pf = cap.preflight(agent, capability=capability, message=message)
    if not pf["ok"]:
        return (f"⚠️ Não deleguei ao {agent.capitalize()}: {pf['reason']}.\n"
                f"➡️ {pf['suggestion']}\n"
                f"EVIDÊNCIA: {json.dumps({'action':'delegate.preflight','status':'blocked','agent':agent,'reason':pf['reason']}, ensure_ascii=False)}")

    timeout = FLASH_TIMEOUT if agent == "flash" else ALEX_TIMEOUT
    r = _resolve_routing(origin_chat_id)
    task, reused = ts.create_task("garra", agent, capability, message,
                                  timeout_secs=timeout, notify_chat_id=r["notify_chat_id"],
                                  monitor=True, origin_chat_id=r["origin_chat_id"],
                                  origin_channel=r["origin_channel"], bot_id=r["bot_id"],
                                  message_thread_id=r["message_thread_id"],
                                  delivery_scope=r["delivery_scope"])
    tid = task["task_id"]

    if reused:
        cur = ts.get_task(tid)
        return (f"♻️ Já existe uma tarefa equivalente em andamento para o {agent.capitalize()} "
                f"(task_id {tid}, status {cur['status']}). Não criei duplicata — acompanhe com "
                f"`check_task('{tid}')`.\n{_ev(cur)}")

    _spawn_worker(tid)
    # fast path: give quick tasks a chance to finish inline
    final = _wait_terminal(tid, FAST_WAIT_SECS)

    if final and final["status"] == "succeeded":
        ts.set_monitor(tid, active=False)   # delivered inline; no monitor needed
        return (f"✅ {agent.capitalize()} concluiu (task_id {tid}).\n\n"
                f"--- resposta do {agent.capitalize()} ---\n{final['result']}\n--- fim ---\n{_ev(final)}")
    if final and final["status"] in ("failed", "timed_out", "cancelled"):
        ts.set_monitor(tid, active=False)
        return (f"❌ {agent.capitalize()} não concluiu (task_id {tid}, status {final['status']}). "
                f"Erro: {final.get('error')}\n{_ev(final)}")

    # still running -> hand off to recurring monitor (heartbeat), do NOT fake progress
    cur = ts.get_task(tid)
    return (f"🛠️ Tarefa delegada ao {agent.capitalize()} e EM EXECUÇÃO de verdade "
            f"(task_id {tid}, status {cur['status']}, worker_pid {cur['worker_pid']}).\n"
            f"Um monitor recorrente (a cada 10 min) acompanha e avisará no Telegram quando houver "
            f"progresso, erro ou conclusão. Consulte agora com `check_task('{tid}')`.\n{_ev(cur)}")


@mcp.tool()
def ask_flash(message: str, garra_origin_chat_id: str = "", garra_session_id: str = "") -> str:
    """Delega uma tarefa de ENGENHARIA ao Flash (Claude Code) — lê/escreve código,
    roda comandos, configura, depura e conserta. NÃO bloqueia: cria uma tarefa
    rastreável (task_id) executada por um worker real; tarefas curtas já retornam a
    resposta; tarefas longas seguem com monitor. Sempre retorna evidência (task_id,
    status). NUNCA afirme que o Flash está trabalhando sem o task_id desta resposta.

    NÃO preencha garra_origin_chat_id/garra_session_id: são INJETADOS pelo sistema
    (o chat real da conversa) para que as notificações voltem ao mesmo chat."""
    msg = (message or "").strip()
    if not msg:
        return "Erro: mensagem vazia — diga o que pedir ao Flash."
    return _delegate("flash", msg, "engineering",
                     origin_chat_id=garra_origin_chat_id, origin_session_id=garra_session_id)


@mcp.tool()
def ask_alex(message: str, garra_origin_chat_id: str = "", garra_session_id: str = "") -> str:
    """Delega ao Alex (assistente especialista Hermes/deepseek) tarefas de ANÁLISE,
    planejamento, coordenação e escrita. Atenção: o Alex NÃO tem Gmail/Calendar/
    Telegram — pedidos de e-mail são recusados e redirecionados para as ferramentas
    da própria Garra. Retorna task_id + status + evidência (sem inventar progresso).

    NÃO preencha garra_origin_chat_id/garra_session_id: são INJETADOS pelo sistema."""
    msg = (message or "").strip()
    if not msg:
        return "Erro: mensagem vazia — diga o que perguntar ao Alex."
    return _delegate("alex", msg, "analysis",
                     origin_chat_id=garra_origin_chat_id, origin_session_id=garra_session_id)


@mcp.tool()
def check_task(task_id: str) -> str:
    """Consulta o STATUS REAL de uma tarefa delegada (NÃO inicia outra).

    Tem trava anti-polling: consultas idênticas repetidas sem novidade retornam
    BLOCKED (com a próxima janela liberada) em vez de entrar em loop — confie no
    monitor recorrente, que avisa no Telegram quando o status muda."""
    t = ts.get_task(task_id)
    if not t:
        return (f"Tarefa {task_id} não encontrada (UNVERIFIED — id não existe no store).\n"
                f"EVIDÊNCIA: {json.dumps({'status':'unknown','verdict':'UNVERIFIED','task_id':task_id}, ensure_ascii=False)}")

    decision = ts.register_check(task_id)
    if not decision.get("allowed"):
        return (f"⛔ BLOCKED — {decision.get('reason')}.\n"
                f"Tarefa {task_id} segue {decision.get('status')} "
                f"(consultas={decision.get('check_count')}, próxima liberada após "
                f"{decision.get('next_check_at')}). NÃO consulte de novo agora; o monitor "
                f"recorrente avisa no Telegram quando mudar.\n"
                f"EVIDÊNCIA: {json.dumps(decision, ensure_ascii=False)}")

    events = ts.get_events(task_id, limit=8)
    last = "; ".join(f"{e['event']}" for e in events[::-1])
    head = (f"Tarefa {task_id} → {t['status']} (agente {t['assigned_agent']}). "
            f"criada {t['created_at']}, hb {t['last_heartbeat_at']}, timeout {t['timeout_at']}.\n"
            f"eventos: {last}\n")
    if t["status"] == "succeeded" and t["result"]:
        head += f"--- resposta ---\n{t['result'][:1500]}\n--- fim ---\n"
    elif t["status"] in ("failed", "timed_out"):
        head += f"erro: {t['error']}\n"
    ld = ts.last_delivered(task_id)
    if ld:
        head += f"entrega Telegram: message_id {ld['message_id']} para {ld['chat_masked']}\n"
    return head + _ev(t)


@mcp.tool()
def verify_task(task_id: str) -> str:
    """Verifica se um identificador de tarefa é REAL (existe no store autorizado).

    Use isto antes de citar qualquer task_id ao usuário. Retorna PASS (com
    evidência) ou UNVERIFIED (id inexistente — NÃO o apresente como real)."""
    v = ts.verify_identifier(task_id)
    return f"{v['verdict']} — task {task_id}\nEVIDÊNCIA: {json.dumps(v, ensure_ascii=False)}"


@mcp.tool()
def schedule_heartbeat(note: str = "ping", garra_origin_chat_id: str = "",
                       garra_session_id: str = "") -> str:
    """Registra um heartbeat REAL do monitor Python (NÃO o schedule_heartbeat
    nativo do binário Rust, que nunca persistia). Cria uma tarefa rastreável,
    entrega uma notificação no Telegram NO CHAT DA CONVERSA (não no owner fixo) e
    persiste o message_id real, validando que o chat de entrega bate com o destino.

    NÃO preencha garra_origin_chat_id/garra_session_id: são INJETADOS pelo sistema."""
    msg = (note or "ping").strip()[:500]
    r = _resolve_routing(garra_origin_chat_id)
    task, reused = ts.create_task("garra", "monitor", "heartbeat", msg,
                                  timeout_secs=60, notify_chat_id=r["notify_chat_id"],
                                  monitor=False, origin_chat_id=r["origin_chat_id"],
                                  origin_channel=r["origin_channel"], bot_id=r["bot_id"],
                                  message_thread_id=r["message_thread_id"],
                                  delivery_scope=r["delivery_scope"])
    tid = task["task_id"]
    if reused:
        return (f"♻️ Heartbeat equivalente recente (task_id {tid}). {_ev(ts.get_task(tid))}")
    ts.mark_running(tid)
    chat = r["notify_chat_id"]
    masked = notify.mask_chat(chat)
    ts.record_notification(tid, "delivery_pending", chat_masked=masked, attempt=1, kind="heartbeat")
    res = notify.send_message(chat, f"💓 Heartbeat Garra (monitor Python): {msg}")
    delivered = bool(res.get("ok") and res.get("message_id") is not None)
    mismatch = delivered and res.get("delivered_chat_id") not in (None, str(chat))
    if delivered and not mismatch:
        ts.record_notification(tid, "delivered", chat_masked=masked,
                               message_id=res["message_id"], attempt=1, kind="heartbeat")
        ts.mark_succeeded(tid, f"heartbeat entregue (message_id {res['message_id']} "
                               f"→ chat {chat}, scope {r['delivery_scope']})")
        return (f"✅ Heartbeat entregue (task_id {tid}, message_id {res['message_id']} → {masked}, "
                f"scope {r['delivery_scope']}).\n{_ev(ts.get_task(tid))}")
    err = "chat_mismatch" if mismatch else res.get("error")
    ts.record_notification(tid, "delivery_failed", chat_masked=masked, attempt=1,
                           error=err, kind="heartbeat")
    ts.mark_failed(tid, f"heartbeat NÃO entregue: {err}")
    return (f"❌ Heartbeat NÃO entregue (task_id {tid}): {err}. "
            f"Não afirmei entrega sem message_id válido no chat certo.\n{_ev(ts.get_task(tid))}")


@mcp.tool()
def get_task_result(task_id: str) -> str:
    """Retorna o RESULTADO COMPLETO de uma tarefa concluída (status succeeded)."""
    t = ts.get_task(task_id)
    if not t:
        return f"Tarefa {task_id} não encontrada."
    if t["status"] != "succeeded":
        return f"Tarefa {task_id} ainda não concluída (status {t['status']}). {_ev(t)}"
    return f"Resultado da tarefa {task_id} ({t['assigned_agent']}):\n\n{t['result']}\n\n{_ev(t)}"


@mcp.tool()
def list_tasks(active_only: bool = True) -> str:
    """Lista tarefas delegadas (por padrão só as ativas). Use para evitar duplicar."""
    rows = ts.list_tasks(active_only=active_only, limit=15)
    if not rows:
        return "Nenhuma tarefa " + ("ativa." if active_only else "encontrada.")
    out = [f"{len(rows)} tarefa(s):"]
    for t in rows:
        out.append(f"- {t['task_id']} [{t['status']}] {t['assigned_agent']} :: {(t['payload'] or '')[:60]}")
    return "\n".join(out)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancela uma tarefa em andamento (marca cancelled e encerra o worker)."""
    t = ts.get_task(task_id)
    if not t:
        return f"Tarefa {task_id} não encontrada."
    if t["status"] in ts.TERMINAL:
        return f"Tarefa {task_id} já está {t['status']}."
    if t["worker_pid"]:
        try:
            os.killpg(os.getpgid(t["worker_pid"]), signal.SIGKILL)
        except Exception:
            pass
    ts.mark_cancelled(task_id, "cancelled via tool")
    ts.set_monitor(task_id, active=False)
    return f"Tarefa {task_id} cancelada.\n{_ev(ts.get_task(task_id))}"


@mcp.tool()
def agent_capabilities() -> str:
    """Catálogo REAL de capacidades + health check ao vivo de Garra/Flash/Alex/n8n/
    Gmail/Calendar/Telegram. Use ANTES de delegar para confirmar disponibilidade."""
    h = cap.health()
    lines = ["Saúde dos componentes (ao vivo):"]
    for name, c in h.items():
        flag = "✅" if c.get("ok") else "❌"
        extra = c.get("account") or c.get("bot") or c.get("version") or c.get("detail") or ""
        lines.append(f"  {flag} {name}: {extra}")
    lines.append("\nCapacidades:")
    lines.append("  • Garra: Gmail, Calendar, Telegram, filesystem, delegação")
    lines.append("  • Flash: código, infra, n8n, shell, debugging")
    lines.append("  • Alex: análise, planejamento, escrita (SEM Gmail/Calendar/Telegram)")
    lines.append("EVIDÊNCIA: " + json.dumps({k: v.get("ok") for k, v in h.items()}, ensure_ascii=False))
    return "\n".join(lines)


if __name__ == "__main__":
    ts.init_db()
    mcp.run()
