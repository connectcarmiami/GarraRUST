//! Runtime anti-hallucination output guard (GAR-anti-hallucination).
//!
//! The LLM can ignore prompt-level rules and simply *print* operational
//! identifiers — task ids shaped like `t-1f991504fae3`, correlation ids like
//! `corr-abc123def0` — that it never actually obtained from a tool. That is the
//! exact failure Connect Car reported: the model emitted a hand-written
//! "RELATÓRIO DE CRIAÇÃO DE TAREFAS" citing `t-7f4e2c9a1b8d` / `t-8a3f5d7e2c9b`
//! with `status: accepted`, even though no delegation tool was ever called and
//! those ids never existed in the authorized task store.
//!
//! A prompt cannot enforce this; only the runtime can. This module binds every
//! operational identifier in the assistant's final text to *evidence*: the set
//! of identifiers that genuinely appeared in this turn's tool-call results (plus
//! the identifiers the user themselves typed). Any identifier with no backing
//! evidence is redacted before the reply is delivered to the channel or
//! persisted to the session store, so a fabricated id can never reach the user
//! as if it were real.
//!
//! Scope is deliberately conservative — only the two identifier shapes garraia
//! actually mints (`t-<hex>` and `corr-<alnum>`) are matched. Bare integers and
//! ISO timestamps are intentionally left untouched to avoid redacting legitimate
//! numbers in normal prose.
//!
//! Behaviour is controlled by `GARRA_OUTPUT_GUARD`:
//!   - `redact` (default) — replace unverified ids with a marker + append a note;
//!   - `block`            — replace the whole reply with a safe refusal;
//!   - `off`              — disable (for debugging only; not recommended).

use std::collections::HashSet;

use tracing::warn;

/// Visible marker that replaces a fabricated identifier. No MarkdownV2 special
/// characters, so it survives Telegram formatting unescaped.
const REDACTION: &str = "ID-NÃO-VERIFICADO";

/// Minimum / maximum body length for a `t-<hex>` task id. Real ids are
/// `t-` + 12 hex chars; we accept 8..=16 to be robust to future widths.
const TASK_MIN: usize = 8;
const TASK_MAX: usize = 16;
/// `corr-<alnum>` correlation ids are `corr-` + 10 chars; accept 6..=16.
const CORR_MIN: usize = 6;
const CORR_MAX: usize = 16;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardMode {
    Off,
    Redact,
    Block,
}

impl GuardMode {
    pub fn from_env() -> Self {
        match std::env::var("GARRA_OUTPUT_GUARD")
            .ok()
            .as_deref()
            .map(str::trim)
        {
            Some("off") => GuardMode::Off,
            Some("block") => GuardMode::Block,
            // Any other value (including unset / "redact") → safe default.
            _ => GuardMode::Redact,
        }
    }
}

/// A detected operational identifier and its byte span within the source text.
#[derive(Debug, Clone)]
struct Span {
    start: usize,
    end: usize,
    token: String,
}

/// `true` when the char immediately before an identifier is a valid left
/// boundary (start of string or a non-identifier char), so we never match the
/// tail of a larger token.
fn left_boundary(prev: Option<char>) -> bool {
    match prev {
        None => true,
        Some(c) => !(c.is_ascii_alphanumeric() || c == '-' || c == '_'),
    }
}

/// `true` when the char immediately after an identifier body is a valid right
/// boundary.
fn right_boundary(next: Option<char>) -> bool {
    match next {
        None => true,
        Some(c) => !(c.is_ascii_alphanumeric() || c == '-' || c == '_'),
    }
}

/// Find all occurrences of `prefix` followed by a `body_ok` run whose length is
/// within `[min, max]`, delimited by identifier boundaries.
fn scan_prefix(
    text: &str,
    prefix: &str,
    min: usize,
    max: usize,
    body_ok: fn(char) -> bool,
    out: &mut Vec<Span>,
) {
    let plen = prefix.len();
    let mut from = 0usize;
    while let Some(rel) = text[from..].find(prefix) {
        let start = from + rel;
        let body_start = start + plen;
        let prev = text[..start].chars().next_back();

        // Default next search position; refined to `end` on a real match.
        let mut advance = body_start.max(start + 1);

        if left_boundary(prev) {
            let mut end = body_start;
            let mut body_len = 0usize;
            for (i, c) in text[body_start..].char_indices() {
                if body_ok(c) {
                    end = body_start + i + c.len_utf8();
                    body_len += 1;
                    if body_len > max {
                        break;
                    }
                } else {
                    break;
                }
            }
            let next = text[end..].chars().next();
            if body_len >= min && body_len <= max && right_boundary(next) {
                out.push(Span {
                    start,
                    end,
                    token: text[start..end].to_string(),
                });
                advance = end;
            }
        }
        from = advance;
    }
}

/// Scan `text` for every operational identifier garraia mints.
fn scan(text: &str) -> Vec<Span> {
    let mut spans = Vec::new();
    // Task ids: t-<hex>
    scan_prefix(text, "t-", TASK_MIN, TASK_MAX, |c| c.is_ascii_hexdigit(), &mut spans);
    // Correlation ids: corr-<alnum>
    scan_prefix(
        text,
        "corr-",
        CORR_MIN,
        CORR_MAX,
        |c| c.is_ascii_alphanumeric(),
        &mut spans,
    );
    spans.sort_by_key(|s| s.start);
    spans
}

/// Harvest the identifiers that appear in *trusted* text (tool-call results or
/// the user's own message) into the per-turn verified set.
pub fn collect_verified_ids(text: &str, set: &mut HashSet<String>) {
    for s in scan(text) {
        set.insert(s.token);
    }
}

/// Outcome of guarding one assistant reply.
#[derive(Debug, Clone)]
pub struct GuardReport {
    pub sanitized: String,
    /// Distinct unverified identifiers that were removed.
    pub removed: Vec<String>,
}

/// Core, side-effect-free enforcement: replace every identifier in `response`
/// that is absent from `verified` with [`REDACTION`]. Returns the sanitized
/// text and the list of removed identifiers (deduped, in first-seen order).
pub fn enforce(response: &str, verified: &HashSet<String>) -> GuardReport {
    let spans = scan(response);
    let mut removed_set: HashSet<String> = HashSet::new();
    let mut removed: Vec<String> = Vec::new();
    let mut out = String::with_capacity(response.len());
    let mut cursor = 0usize;

    for s in &spans {
        if verified.contains(&s.token) {
            continue; // backed by real evidence — keep verbatim
        }
        // Copy the untouched gap before this span, then the redaction marker.
        out.push_str(&response[cursor..s.start]);
        out.push_str(REDACTION);
        cursor = s.end;
        if removed_set.insert(s.token.clone()) {
            removed.push(s.token.clone());
        }
    }
    out.push_str(&response[cursor..]);

    GuardReport {
        sanitized: out,
        removed,
    }
}

/// Apply the guard to a final assistant reply according to `GARRA_OUTPUT_GUARD`.
/// This is the function the runtime calls at every `return Ok(text)` site.
pub fn guard(response: &str, verified: &HashSet<String>) -> String {
    guard_with_mode(response, verified, GuardMode::from_env())
}

/// Mode-explicit core of [`guard`], so behaviour is unit-testable without
/// mutating the process environment.
pub fn guard_with_mode(response: &str, verified: &HashSet<String>, mode: GuardMode) -> String {
    if mode == GuardMode::Off {
        return response.to_string();
    }

    let report = enforce(response, verified);
    if report.removed.is_empty() {
        return response.to_string();
    }

    warn!(
        removed = report.removed.len(),
        ids = ?report.removed,
        "output_guard: redacted unverified operational identifier(s) — possible hallucination"
    );

    match mode {
        GuardMode::Block => format!(
            "⚠️ Bloqueei minha própria resposta: ela citava {n} identificador(es) de \
             tarefa/correlação que NÃO vieram de nenhuma ferramenta real nesta interação \
             (possível alucinação). Não vou apresentá-los como reais. Consulte o status \
             verdadeiro com as ferramentas de delegação (delegation__check_task / \
             delegation__list_tasks).",
            n = report.removed.len()
        ),
        // Redact (default): keep the useful prose, neutralise the fake ids, warn.
        _ => format!(
            "{sanitized}\n\n⚠️ Aviso anti-alucinação (sistema): removi {n} identificador(es) \
             que não vieram de nenhuma ferramenta real nesta interação. Eles foram marcados \
             como “{marker}”. Para status real, use delegation__check_task / \
             delegation__list_tasks.",
            sanitized = report.sanitized,
            n = report.removed.len(),
            marker = REDACTION
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn real_task_id_backed_by_evidence_passes_through() {
        // The id appeared in a tool result this turn → verified → kept verbatim.
        let verified = set(&["t-1f991504fae3"]);
        let resp = "A tarefa t-1f991504fae3 foi concluída com sucesso.";
        let out = enforce(resp, &verified);
        assert!(out.removed.is_empty());
        assert_eq!(out.sanitized, resp);
    }

    #[test]
    fn invented_task_id_is_redacted() {
        // The exact ids the model fabricated for Connect Car, with no evidence.
        let verified = set(&[]);
        let resp = "Criei as tarefas t-7f4e2c9a1b8d e t-8a3f5d7e2c9b com status accepted.";
        let out = enforce(resp, &verified);
        assert_eq!(out.removed.len(), 2);
        assert!(!out.sanitized.contains("t-7f4e2c9a1b8d"));
        assert!(!out.sanitized.contains("t-8a3f5d7e2c9b"));
        assert!(out.sanitized.contains(REDACTION));
    }

    #[test]
    fn mixed_real_and_fake_keeps_real_redacts_fake() {
        let verified = set(&["t-1f991504fae3"]);
        let resp = "Status: t-1f991504fae3 ok; a nova t-7f4e2c9a1b8d em execução.";
        let out = enforce(resp, &verified);
        assert_eq!(out.removed, vec!["t-7f4e2c9a1b8d".to_string()]);
        assert!(out.sanitized.contains("t-1f991504fae3"));
        assert!(!out.sanitized.contains("t-7f4e2c9a1b8d"));
    }

    #[test]
    fn correlation_id_is_guarded() {
        let verified = set(&[]);
        let resp = "Evidência corr-abc123def0 confirmada.";
        let out = enforce(resp, &verified);
        assert_eq!(out.removed, vec!["corr-abc123def0".to_string()]);
        assert!(out.sanitized.contains(REDACTION));
    }

    #[test]
    fn bare_numbers_and_words_are_not_false_positives() {
        // message_id integers, ISO timestamps, ordinary words must survive.
        let verified = set(&[]);
        let resp = "message_id 123456789, em 2026-06-18T20:54:00Z, t-shirt e short.";
        let out = enforce(resp, &verified);
        assert!(out.removed.is_empty(), "unexpected removals: {:?}", out.removed);
        assert_eq!(out.sanitized, resp);
    }

    #[test]
    fn collect_seeds_verified_set_so_user_echo_is_allowed() {
        // If the user typed the id, the assistant may echo it.
        let mut verified = HashSet::new();
        collect_verified_ids("qual o status de t-1f991504fae3 ?", &mut verified);
        let resp = "A t-1f991504fae3 segue em execução.";
        let out = enforce(resp, &verified);
        assert!(out.removed.is_empty());
        assert_eq!(out.sanitized, resp);
    }

    #[test]
    fn too_short_or_too_long_bodies_are_ignored() {
        let verified = set(&[]);
        // 4 hex (too short) and 20 hex (too long) → not minted-id shaped.
        let resp = "t-dead and t-0123456789abcdef0123 are not ids";
        let out = enforce(resp, &verified);
        assert!(out.removed.is_empty(), "unexpected: {:?}", out.removed);
    }

    #[test]
    fn guard_appends_warning_note_in_redact_mode() {
        let verified = set(&[]);
        let out = guard_with_mode("feito em t-7f4e2c9a1b8d", &verified, GuardMode::Redact);
        assert!(out.contains(REDACTION));
        assert!(out.contains("anti-alucinação"));
        assert!(!out.contains("t-7f4e2c9a1b8d"));
    }

    #[test]
    fn guard_block_mode_refuses_without_leaking_ids() {
        let verified = set(&[]);
        let out = guard_with_mode("criei t-7f4e2c9a1b8d", &verified, GuardMode::Block);
        assert!(!out.contains("t-7f4e2c9a1b8d"));
        assert!(out.contains("Bloqueei"));
    }

    #[test]
    fn guard_off_mode_is_passthrough() {
        let verified = set(&[]);
        let out = guard_with_mode("criei t-7f4e2c9a1b8d", &verified, GuardMode::Off);
        assert_eq!(out, "criei t-7f4e2c9a1b8d");
    }
}
