//! End-to-end proof that the anti-hallucination output guard fires through the
//! REAL agent loop (`process_message_streaming_with_agent_config`), not only the
//! pure helper. A stub provider returns text that fabricates the exact task ids
//! Connect Car's Garra invented; the guard must redact them before the runtime
//! hands the reply back to the channel.

use std::sync::Arc;

use async_trait::async_trait;
use garraia_agents::providers::{ContentBlock, LlmProvider, LlmRequest, LlmResponse};
use garraia_agents::AgentRuntime;
use garraia_common::Result;
use tokio::sync::mpsc;

/// A provider that always answers with a fixed string and never calls a tool —
/// i.e. it can only *claim*, never produce evidence.
struct FabricatingProvider {
    text: String,
}

#[async_trait]
impl LlmProvider for FabricatingProvider {
    fn provider_id(&self) -> &str {
        "fabricator"
    }
    async fn complete(&self, _request: &LlmRequest) -> Result<LlmResponse> {
        Ok(LlmResponse {
            content: vec![ContentBlock::Text {
                text: self.text.clone(),
            }],
            model: "fabricator".to_string(),
            usage: None,
            stop_reason: Some("stop".to_string()),
        })
    }
    async fn health_check(&self) -> Result<bool> {
        Ok(true)
    }
}

fn block_on<F: std::future::Future>(f: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(f)
}

async fn drive(user_text: &str, model_text: &str) -> String {
    let runtime = AgentRuntime::new();
    runtime.register_provider(Arc::new(FabricatingProvider {
        text: model_text.to_string(),
    }));
    let (tx, _rx) = mpsc::channel::<String>(64);
    runtime
        .process_message_streaming_with_agent_config(
            "sess-guard-test",
            user_text,
            &[],
            tx,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .await
        .expect("runtime returned Ok")
}

#[test]
fn fabricated_task_ids_are_redacted_through_the_real_loop() {
    let out = block_on(drive(
        "crie duas tarefas reais agora",
        "Pronto: criei t-7f4e2c9a1b8d e t-8a3f5d7e2c9b com status accepted.",
    ));
    assert!(!out.contains("t-7f4e2c9a1b8d"), "fabricated id leaked: {out}");
    assert!(!out.contains("t-8a3f5d7e2c9b"), "fabricated id leaked: {out}");
    assert!(
        out.contains("ID-NÃO-VERIFICADO"),
        "expected guard marker, got: {out}"
    );
}

#[test]
fn user_supplied_id_is_preserved() {
    // When the user themselves typed the id, the assistant may echo it.
    let out = block_on(drive(
        "qual o status de t-1f991504fae3?",
        "A tarefa t-1f991504fae3 segue em execução.",
    ));
    assert!(
        out.contains("t-1f991504fae3"),
        "a legitimately user-supplied id was wrongly redacted: {out}"
    );
}
