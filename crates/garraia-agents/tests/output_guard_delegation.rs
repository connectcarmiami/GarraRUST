//! Same-turn reproduction: the model calls a delegation tool that returns real
//! `task_id`/`correlation_id`, then cites them in its final reply. The runtime
//! must harvest those ids from the tool result and let them through the output
//! guard, while still redacting a fabricated id the model invents in the same
//! reply. Drives the REAL streaming agent loop.

use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use futures::Stream;
use garraia_agents::providers::{
    ChatMessage, ChatRole, ContentBlock, LlmProvider, LlmRequest, LlmResponse, MessagePart,
    StreamEvent,
};
use garraia_agents::tools::{Tool, ToolContext, ToolOutput};
use garraia_agents::AgentRuntime;
use garraia_common::Result;
use tokio::sync::mpsc;

const REAL_TASK: &str = "t-6f1bb5be20c7";
const REAL_CORR: &str = "corr-54dfd2205a";
const FAKE_TASK: &str = "t-7f4e2c9a1b8d";

/// Stub of `delegation__ask_flash` returning the real, evidence-bearing output.
struct AskFlashStub;

#[async_trait]
impl Tool for AskFlashStub {
    fn name(&self) -> &str {
        "delegation__ask_flash"
    }
    fn description(&self) -> &str {
        "stub delegation tool"
    }
    fn input_schema(&self) -> serde_json::Value {
        serde_json::json!({"type":"object","properties":{"message":{"type":"string"}}})
    }
    async fn execute(&self, _ctx: &ToolContext, _input: serde_json::Value) -> Result<ToolOutput> {
        Ok(ToolOutput::success(format!(
            "🛠️ Tarefa delegada ao Flash e EM EXECUÇÃO de verdade (task_id {REAL_TASK}, \
             status running, worker_pid 12345). Consulte com `check_task('{REAL_TASK}')`.\n\
             EVIDÊNCIA: {{\"task_id\": \"{REAL_TASK}\", \"agent\": \"flash\", \
             \"correlation_id\": \"{REAL_CORR}\"}}"
        )))
    }
}

/// Streaming provider: call 1 emits a tool_use for ask_flash; call 2 emits the
/// final reply citing the real ids plus a fabricated one.
struct ScriptedStreamingProvider {
    calls: AtomicUsize,
    final_text: String,
}

#[async_trait]
impl LlmProvider for ScriptedStreamingProvider {
    fn provider_id(&self) -> &str {
        "scripted"
    }
    async fn complete(&self, _request: &LlmRequest) -> Result<LlmResponse> {
        // Not used: streaming path is exercised.
        Ok(LlmResponse {
            content: vec![ContentBlock::Text { text: String::new() }],
            model: "scripted".into(),
            usage: None,
            stop_reason: Some("stop".into()),
        })
    }
    async fn stream_complete(
        &self,
        _request: &LlmRequest,
    ) -> Result<Pin<Box<dyn Stream<Item = Result<StreamEvent>> + Send>>> {
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        let events: Vec<Result<StreamEvent>> = if n == 0 {
            vec![
                Ok(StreamEvent::ToolUseStart {
                    index: 0,
                    id: "call-1".into(),
                    name: "delegation__ask_flash".into(),
                }),
                Ok(StreamEvent::InputJsonDelta("{\"message\":\"faça X\"}".into())),
                Ok(StreamEvent::ContentBlockStop { index: 0 }),
                Ok(StreamEvent::MessageStop),
            ]
        } else {
            vec![
                Ok(StreamEvent::TextDelta(self.final_text.clone())),
                Ok(StreamEvent::MessageStop),
            ]
        };
        Ok(Box::pin(futures::stream::iter(events)))
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

#[test]
fn same_turn_delegation_ids_pass_fabricated_redacted() {
    let final_text = format!(
        "✅ Deleguei ao Flash.\n- task_id: {REAL_TASK}\n- correlation_id: {REAL_CORR}\n\
         (e a task antiga {FAKE_TASK} que eu lembrava)"
    );
    let mut runtime = AgentRuntime::new();
    runtime.register_tool(Box::new(AskFlashStub));
    runtime.register_provider(Arc::new(ScriptedStreamingProvider {
        calls: AtomicUsize::new(0),
        final_text,
    }));

    let (tx, _rx) = mpsc::channel::<String>(64);
    let out = block_on(runtime.process_message_streaming_with_agent_config(
        "sess-deleg",
        "delegue ao flash",
        &[],
        tx,
        None,
        None,
        None,
        None,
        None,
        None,
    ))
    .expect("runtime ok");

    // Real ids from the tool result must survive...
    assert!(out.contains(REAL_TASK), "real task_id was redacted! out: {out}");
    assert!(out.contains(REAL_CORR), "real correlation_id was redacted! out: {out}");
    // ...the fabricated one must be redacted.
    assert!(!out.contains(FAKE_TASK), "fabricated id leaked! out: {out}");
    assert!(out.contains("ID-NÃO-VERIFICADO"), "expected redaction marker, out: {out}");
}

#[test]
fn req9_5_prior_turn_id_in_text_only_history_is_redacted() {
    // The id appears in history ONLY as assistant TEXT (its tool result has
    // scrolled out of context). Citing it again WITHOUT re-calling a tool must
    // be redacted — assistant prose is never evidence (req 5). To show it again,
    // Garra must re-fetch via list_tasks/check_task (a fresh ToolResult).
    let mut runtime = AgentRuntime::new();
    runtime.register_provider(Arc::new(ScriptedStreamingProvider {
        calls: AtomicUsize::new(1), // start at 1 → final-text branch, no tool call
        final_text: format!("O status da task {REAL_TASK} é running."),
    }));
    let history = vec![ChatMessage {
        role: ChatRole::Assistant,
        content: MessagePart::Text(format!("Antes deleguei a task {REAL_TASK}.")),
    }];
    let (tx, _rx) = mpsc::channel::<String>(64);
    let out = block_on(runtime.process_message_streaming_with_agent_config(
        "sess-prior", "e o status?", &history, tx, None, None, None, None, None, None,
    ))
    .expect("runtime ok");
    assert!(
        !out.contains(REAL_TASK),
        "prior-turn text-only id must be redacted (req 5), out: {out}"
    );
    assert!(out.contains("ID-NÃO-VERIFICADO"));
}
