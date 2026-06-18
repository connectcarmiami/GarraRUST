# Relatório — Correção definitiva das alucinações operacionais do Garra (Connect Car LLC)

Data: 2026-06-18 · Ambiente real, validado tecnicamente. Sem segredos expostos.
Escopo: IDs de tarefa inventados, progresso falso, polling infinito, heartbeat
"concluído" sem entrega.

> Critério de aceite (do pedido): só declarar resolvido após **provar que IDs
> inventados não podem aparecer como reais** e que **duas notificações
> automáticas chegaram com message_id persistido**.

---

## 1. Causa raiz (o que realmente acontecia)

O reparo anterior (18/06, manhã) montou um task store com evidência, monitor por
timer e um "contrato anti-alucinação" — **mas o contrato era só texto no system
prompt**. Nada no runtime verificava o que o modelo escrevia.

O modelo (deepseek-v4-flash, via OpenRouter, no binário Rust `garra`) podia
simplesmente **digitar** um `task_id` e um bloco `EVIDÊNCIA:` em prosa livre, sem
nunca chamar nenhuma ferramenta. Foi exatamente o que ocorreu: a mensagem do
assistente nº `83ede8b1` (sessão `d6cba934`) imprimiu uma tabela
"RELATÓRIO DE CRIAÇÃO DE TAREFAS" com:

| Campo | Valor impresso | Realidade |
|------|----------------|-----------|
| task_id | `t-7f4e2c9a1b8d` | **0 linhas** em `tasks`/`task_events` |
| task_id | `t-8a3f5d7e2c9b` | **0 linhas** em `tasks`/`task_events` |
| status | `accepted` | nenhuma ferramenta foi chamada |
| horário | `2026-06-18T20:54:00Z` (futuro) | inventado |

Prova forense: os dois IDs aparecem **apenas** em `sessions.db` (log de conversa,
7× cada), e **nunca** no banco autorizado `tasks.db`. O ID real da própria
investigação (`t-1f991504fae3`) existe em `tasks.db` com 21 eventos.

Os outros sintomas decorrem da mesma falta de vínculo runtime↔evidência:
- **polling infinito** de `delegation__check_task`: a ferramenta respondia o
  status toda vez, sem trava; o modelo repetia indefinidamente.
- **heartbeat "entregue" sem mensagem**: o `notify`/`monitor` registrava apenas
  `ok=...` num evento, **não** o `message_id` do Telegram, e marcava o monitor
  como concluído mesmo sem confirmação real do canal. (O `schedule_heartbeat`
  nativo do binário grava em `scheduled_tasks`, que estava com **0 linhas** —
  nunca rodou.)

---

## 2. Arquitetura real (apurada, sem confiar no relato do Garra)

- Binário em produção: `~/.local/bin/garraia` = cópia de
  `GarraRUST/target/release/garra` (ELF 48 MB, build 2026-06-13).
  Serviço: `garraia.service` (systemd user) → `garra start`.
- Fonte Rust: `~/Documents/Projetos/GarraRUST` (git, branch `main`, remoto
  `connectcarmiami/GarraRUST`). Loop do agente:
  `crates/garraia-agents/src/runtime.rs`. Resultado de ferramenta =
  `ContentBlock::ToolResult{ content }`. Entrega Telegram:
  `garraia-channels/src/telegram.rs` — a **edição final** da mensagem usa o
  valor de retorno do loop (`full_response`); `persist_turn` grava o mesmo valor.
- Delegação (Python, roda na venv do Hermes): `~/.config/garraia/` —
  `delegation_mcp.py` (servidor MCP `delegation`) + pacote `garra_delegation/`
  (`taskstore.py`, `agent_worker.py`, `monitor.py`, `notify.py`,
  `capabilities.py`). Banco: `~/.garraia/data/tasks.db`.
- Monitor recorrente: `garra-monitor.timer` (a cada 10 min) →
  `python -m garra_delegation.monitor`.

---

## 3. Correções implementadas

### 3.1 Runtime (Rust) — guarda de saída que liga identificador a evidência

Novo módulo `crates/garraia-agents/src/output_guard.rs` + fiação em `runtime.rs`:

- Para cada turno, monta-se um conjunto **verificado** de identificadores: os que
  aparecem nos **resultados reais de ferramenta** do turno + os que o **usuário**
  digitou. Prosa do assistente nunca é fonte de evidência.
- Antes de **todo** retorno de texto do modelo (caminhos streaming e
  não-streaming), `output_guard::guard()` varre a resposta por identificadores
  que o sistema emite (`t-<hex>`, `corr-<alnum>`) e **redige** (`ID-NÃO-VERIFICADO`)
  qualquer um ausente do conjunto verificado, anexando um aviso. Modo configurável
  `GARRA_OUTPUT_GUARD = redact|block|off` (padrão `redact`).
- Como a edição final do Telegram e o `persist_turn` usam o valor retornado, um ID
  inventado **não chega** ao usuário nem ao banco como real.
- Escopo conservador: inteiros soltos e timestamps ISO **não** são marcados (zero
  falso-positivo verificado em teste).

### 3.2 Persistência (Python) — `taskstore.py`

- **Máquina de estados** explícita (`requested/queued → accepted → claimed →
  running → succeeded|failed|timed_out|cancelled`), com transições legais
  validadas em `_transition()`; estado terminal é "pegajoso" (não há como
  "ressuscitar" para `running`).
- **`succeeded` exige resultado**: `mark_succeeded()` lança `MissingResult` se o
  resultado for vazio; grava `provenance`.
- `verify_identifier()` — autoridade que transforma "o modelo digitou t-xxxx" em
  **PASS** (existe no store) ou **UNVERIFIED** (não existe).
- Ledger de entrega `notifications` (estados
  `response_generated/delivery_pending/delivered/delivery_failed`,
  `message_id`, `attempt`, destino mascarado).
- Trava de polling `register_check()` — backoff `[10,20,40,80,160,300]s`,
  `next_check_at`, `MAX_CHECKS=8`; consulta idêntica sem novidade vira **BLOCKED**.

### 3.3 Notificações/heartbeat — `notify.py` + `monitor.py`

- `notify.send_message()` retorna estruturado (`ok`, `message_id`, destino
  mascarado, `error`); só há "delivered" com `message_id` real.
- `monitor.scan_once()` separa os estados de entrega, **persiste o message_id**,
  e só desativa o monitor quando a entrega é confirmada **ou** o retry (limitado a
  `MAX_DELIVERY_ATTEMPTS=3`) se esgota — falha de envio é reentregue, nunca
  silenciada.

### 3.4 Ferramentas de delegação — `delegation_mcp.py`

- `check_task` usa a trava: retorna **BLOCKED** (com próxima janela) em vez de
  loop.
- `verify_task(task_id)` → PASS/UNVERIFIED com evidência.
- `schedule_heartbeat(note)` → heartbeat **real** do monitor Python (distinto do
  `schedule_heartbeat` nativo do Rust que nunca persistia): cria task rastreável,
  entrega no Telegram e persiste o `message_id`.

---

## 4. Arquivos alterados / criados

Rust (no PR, branch `fix/anti-hallucination-output-guard`, commit `d49bee4`):
- `crates/garraia-agents/src/output_guard.rs` (novo, ~354 linhas)
- `crates/garraia-agents/src/runtime.rs` (guarda nos retornos + montagem do set)
- `crates/garraia-agents/src/lib.rs` (`pub mod output_guard;`)
- `crates/garraia-agents/tests/output_guard_runtime.rs` (novo, teste E2E do loop)

Python (deploy em `~/.config/garraia/`, snapshot em
`~/garra-fix/backups/antihallucination-*/`):
- `garra_delegation/taskstore.py`, `notify.py`, `monitor.py`, `delegation_mcp.py`

Testes: `~/garra-fix/scripts/test_antihallucination.py`

---

## 5. Testes e saídas

### 5.1 Rust (`cargo test -p garraia-agents`)
- `output_guard` (unit): **10/10 PASS** — ID inventado redigido; ID real
  preservado; corr-id; sem falso-positivo em números/timestamps; modos
  redact/block/off.
- `output_guard_runtime` (E2E pelo loop real, provider stub que fabrica
  `t-7f4e2c9a1b8d`/`t-8a3f5d7e2c9b`): **2/2 PASS** — IDs fabricados redigidos;
  ID fornecido pelo usuário preservado.

### 5.2 Python (`test_antihallucination.py`) — **13/13 PASS**
ID inventado→UNVERIFIED; tarefa narrada sem chamada→UNVERIFIED; resultado real
citável; check repetido→BLOCKED; dedup; succeeded-sem-resultado rejeitado;
terminal pegajoso; adapter com erro→delivery_failed (não delivered); retry
limitado e monitor desativa ao esgotar; message_id persistido; persistência após
reabrir o banco.

### 5.3 E2E ao vivo — duas notificações automáticas (monitor de produção)
`garra-monitor.service` entregou, no chat autorizado (`******3175`):
- `t-ce7431131e25` → **delivered, message_id 283**
- `t-54651d6f2079` → **delivered, message_id 284**
Ledger `notifications`: `delivery_pending → delivered` para ambos. **PASS.**

---

## 6. Branch / commit / PR
- Branch: `fix/anti-hallucination-output-guard` (pushed)
- Commits:
  - `d49bee4` — `feat(agents): runtime output guard binds reply ids to tool evidence`
  - `42dddae` — `fix(cli): chat REPL displays guard-checked reply, not raw deltas`
- PR: **https://github.com/connectcarmiami/GarraRUST/pull/1**
- As mudanças pré-existentes do `/pair` ficaram **fora** do PR, intactas na árvore.

---

## 7. Estado do deploy — FEITO (backup + build limpo + smoke + rollback)
- Causa do build quebrado: o prefixo OpenSSL `/tmp/localdeps` (headers+pkg-config)
  do build de 13/06 foi **apagado no reboot de 16/06**; sem `pkg-config` nem
  `libssl-dev`, e **sem sudo**. Reconstruí o prefixo via
  `apt-get download libssl-dev` (sem sudo, versão **exata** 3.5.5-1ubuntu3.2) +
  `OPENSSL_LIB_DIR`/`OPENSSL_INCLUDE_DIR` apontando para os headers extraídos e
  symlinks `.so` para o runtime real. Não alterei código/PR por isso.
- Build limpo `cargo build --bin garra --release` (EXIT 0); marcador da guarda
  embutido no binário confirmado.
- Deploy: `~/.local/bin/garraia` ← binário sha `712625da…` (backup do antigo em
  `garraia.binary.bak` + `garraia.binary.PRE-DEPLOY.bak`).
- Restart: `garraia.service` **active**; provider **main healthy (1/1 online)**;
  **telegram channel connected**; gateway em `:3888`; sem erros/panics; processo
  confirmado por `/proc/<pid>/exe` = novo sha.
- **Prova ao vivo da guarda no binário em produção**: drivei o binário real
  (`garra chat`, deepseek/OpenRouter) pedindo ao modelo para **inventar** um
  `task_id`. Resposta entregue:
  `Tarefa **ID-NÃO-VERIFICADO** criada com status accepted.` — id fabricado
  **redigido**, sem vazamento, com aviso anti-alucinação.

### Rollback documentado
- Backup completo: `~/garra-fix/backups/antihallucination-20260618-120937/`
  (DBs WAL-safe, código Python, units systemd, **binário antigo**
  `garraia.binary.bak`).
- Reverter binário: `cp ~/garra-fix/backups/antihallucination-20260618-120937/garraia.binary.bak ~/.local/bin/garraia && systemctl --user restart garraia.service`
- Reverter Python: restaurar `code/garra_delegation/` e `delegation_mcp.py` do
  mesmo backup.
- Desligar só a guarda (sem rebuild): `Environment="GARRA_OUTPUT_GUARD=off"` no
  `garraia.service` + restart.

---

## 8. Matriz de resultados

| Item | Resultado |
|------|-----------|
| ID inventado não pode aparecer como real (runtime) | **PASS** |
| ID real pode ser citado | **PASS** |
| Tarefa narrada sem chamada → UNVERIFIED | **PASS** |
| check_task repetido → BLOCKED (sem loop) | **PASS** |
| succeeded exige resultado | **PASS** |
| Estados de entrega + message_id persistido | **PASS** |
| Duas notificações automáticas com message_id | **PASS** (283, 284) |
| Adapter ausente / Telegram com erro → delivery_failed | **PASS** |
| Retry/idempotência/dedup | **PASS** |
| Deploy do binário com a guarda em produção | **PASS** (sha 712625da, ao vivo) |
| Guarda viva no binário (id inventado redigido em execução real) | **PASS** |

---

## 9. Pendências / observações
- **Superfícies cobertas**: a guarda roda no valor de retorno do runtime. O
  Telegram (produção) entrega exatamente esse valor (edição final +
  `persist_turn`) → limpo. O `garra ask`/`garra_ask` já retornava o valor
  guardado. O REPL `garra chat` foi corrigido (commit `42dddae`) para exibir o
  valor guardado em vez dos deltas crus — verificado ao vivo.
- No Telegram, durante o streaming, um delta intermediário pode exibir o ID por
  ~1s antes da **edição final já redigida** (cosmético; estado final e
  persistido são limpos). Eliminar até o transitório exigiria redigir o stream
  token a token (IDs podem ser partidos entre deltas) — fora de escopo.
- Ambiente de build: o prefixo OpenSSL `/tmp/localdeps` é volátil (perdido no
  reboot). Para rebuilds futuros: restaurar `libssl-dev`+`pkg-config`, ou
  reconstruir o prefixo (passo-a-passo em §7), ou adotar `vendored`/`rustls`.
- O cofre de credenciais (`vault.json`) fica trancado sem
  `GARRAIA_VAULT_PASSPHRASE` no unit, mas isso é **pré-existente** e não-fatal: o
  gateway usa as credenciais inline do `config.yml` (provider + Telegram subiram
  normalmente). Não foi introduzido por esta mudança.
