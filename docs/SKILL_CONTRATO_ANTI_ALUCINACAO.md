# Contrato Anti-Alucinação — Garra / Flash / Alex (Connect Car)

> Sincronizado com o runtime (`GarraRUST/crates/garraia-agents/src/output_guard.rs`
> + `runtime.rs` + `mcp/tool_bridge.rs`), a camada de delegação
> (`~/.config/garraia/garra_delegation/` ↔ `GarraRUST/ops/garra-delegation/`) e o
> system prompt (`~/.config/garraia/config.yml`). Atualizado 2026-06-18.
> Binário em produção validado: `acbc32f1` (output guard + roteamento por origem +
> SSE guardado + anti-laundering). Teste humano Telegram E2E: **PASS**
> (heartbeat real `t-813d357b40de`, `message_id 369` → chat 7978617919,
> `chat_ok=true`, `delivery_scope=origin`, sem `chat_mismatch`).

## 1. Regra de ouro
Garra só pode **afirmar** que algo aconteceu se tiver chamado a ferramenta
correspondente **nesta interação** e recebido a evidência estruturada. Sem
evidência → diga "não verificado" / PARTIAL. **`ID-NÃO-VERIFICADO` NÃO é um
task_id — é um marcador de redação.** Nunca o trate como id real.

## 2. Output guard (runtime, determinístico)
Antes de entregar a resposta final do modelo, o runtime monta um conjunto de IDs
**verificados** e redige (`ID-NÃO-VERIFICADO`) qualquer `t-<hex>` / `corr-<alnum>`
que **não** esteja nesse conjunto.

- O conjunto verificado vem **somente** de:
  1. os blocos `ToolResult` **estruturados** reais do turno atual
     (`harvest_tool_results` / `collect_verified_ids`); e
  2. os IDs que o **próprio usuário** digitou na mensagem.
- **Por turno/conversa, NUNCA global.** O modelo **não** pode adicionar IDs à
  allowlist — só ferramenta real adiciona.
- IDs inventados (`t-falso123`) e **parecidos mas diferentes** (`t-503bb9c8373g`
  vs `t-503bb9c8373f`) continuam redigidos. A regex/validação **não** é relaxada.
- Modos: `GARRA_OUTPUT_GUARD = redact` (padrão) | `block` | `off`. **Nunca** use
  `off` em produção.

### O que PASSA vs o que é REDIGIDO (verificado por teste + smoke)
| Caso | Resultado |
|------|-----------|
| `task_id`/`correlation_id` real vindo de `ask_flash`/`ask_alex`/`list_tasks`/`check_task` **no mesmo turno** | **PASSA** |
| ID inventado pelo modelo | **REDIGIDO** |
| ID parecido (1 char diferente) | **REDIGIDO** |
| ID citado de **memória** (turno anterior, sem rechamar ferramenta) | **REDIGIDO** (esperado) |

> **Regra operacional (cross-turn):** para citar um id de uma mensagem anterior,
> **RE-CONSULTE** com `delegation__list_tasks` ou `delegation__check_task` **nesta
> interação** e copie o id **VERBATIM** da EVIDÊNCIA — só assim ele aparece. Isso
> não é bug: o histórico reconstruído é texto, sem blocos `ToolResult`, então a
> evidência só vale dentro do turno.

### Caminhos confirmados
- **Telegram (streaming)** e **HTTP `/api/sessions/{id}/messages` (não-streaming)**:
  ambos exibem ids reais do mesmo turno e redigem inventados. Smoke streaming
  (`/v1/chat/completions stream:true`, mesmo caminho do Telegram):
  `list_tasks` → 15 ids reais, **0 redações**.
- **SSE/OpenAI guardado (corrigido em `acbc32f1`):** `/v1/chat/completions`
  (stream:true) agora **descarta os deltas crus** e envia ao cliente **apenas o
  texto guardado** (smoke real: id de memória/laundered → redigido, sem
  vazamento). No Telegram a edição final já usava o texto guardado.
- **Anti-laundering (corrigido em `acbc32f1`):** `check_task`/`verify_task`/
  `get_task_result`/`cancel_task` **mascaram ids inexistentes** (`t-aaaab***`,
  não-colhíveis pelo guard) — um id inventado roteado por essas tools não é mais
  "lavado" para verificado; ids reais continuam exibidos.

## 3. Delegação (evidência real)
`ask_flash` (Flash = Claude Code) e `ask_alex` (Alex = Hermes/deepseek) criam uma
**task rastreável** (`tasks.db`) e retornam `task_id` + `correlation_id` reais +
status. O binário injeta `garra_origin_chat_id` (chat real da conversa) nas tools
`delegation__*` para o roteamento de notificação voltar ao chat de origem.

## 4. check_task — auditoria mascarada (sem segredos)
`delegation__check_task(task_id)` devolve metadados auditáveis:
`task_id` real, `status`, `has_result`, `created_at`, `completed_at`, `error`,
`result_ref`/`result_summary`, `origin_chat_id_masked` (ex.: `7978617***`),
`notify_chat_id_masked`, `chat_ids_match`, `delivery_scope`,
`message_id_present` + `message_id`, `chat_ok`. **Nunca** expõe token/segredo.
ID inexistente → `verdict: UNVERIFIED`.

## 5. Monitor / heartbeat
- `timed_out` **só** por sinal real: worker morto (pid ausente) + heartbeat
  parado, com **re-leitura fresca** do status antes de transicionar e proteção
  contra corrida (`InvalidTransition` engolido). **Nunca** inferido de `timeout_at`.
- `succeeded` exige resultado persistido. Entrega só vira `delivered` com
  `message_id` real do Telegram **e** `chat_ok` (chat de entrega == destino);
  divergência → `delivery_failed` / `chat_mismatch`.
- Sem `message_id` real → Monitor fica **PARTIAL**, nunca PASS.

## 6. Estados e veredictos
- Task: `requested/queued → accepted → claimed → running → succeeded|failed|timed_out|cancelled` (terminal é pegajoso).
- Entrega: `response_generated → delivery_pending → delivered | delivery_failed`.
- Veredicto operacional: **PASS** (com evidência) · **PARTIAL** · **BLOCKED** ·
  **UNVERIFIED** · **FAIL**. Nunca declare PASS sem evidência de ToolResult, DB,
  log ou teste.
