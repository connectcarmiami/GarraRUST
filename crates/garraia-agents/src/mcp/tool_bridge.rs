use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use garraia_common::{Error, Result};
use rmcp::model::{CallToolRequestParams, RawContent};
use rmcp::service::{Peer, RoleClient};
use serde_json::Value;
use tracing::info;

use crate::tools::{Tool, ToolContext, ToolOutput};

/// Faz a ponte entre uma ferramenta exposta por um servidor MCP
/// e o trait `Tool` utilizado pelo Garraia.
pub struct McpTool {
    /// Nome com namespace: "nome_servidor.nome_ferramenta"
    nome_completo: String,

    /// Nome original da ferramenta registrada no servidor MCP
    nome_original: String,

    /// Descrição da ferramenta (vinda do servidor MCP)
    descricao: String,

    /// JSON Schema de entrada da ferramenta
    schema_entrada: Value,

    /// Referência compartilhada para o peer MCP
    peer: Arc<Peer<RoleClient>>,

    /// Timeout máximo para execução da ferramenta
    timeout: Duration,
}

impl McpTool {
    pub fn new(
        nome_servidor: &str,
        nome_original: String,
        descricao: Option<String>,
        schema_entrada: Value,
        peer: Arc<Peer<RoleClient>>,
        timeout: Duration,
    ) -> Self {
        Self {
            // Use "__" instead of "." — OpenAI/Anthropic APIs reject dots in tool names
            // (pattern: ^[a-zA-Z0-9_-]+$). The MCP call itself uses `nome_original`.
            nome_completo: format!("{nome_servidor}__{nome_original}"),
            descricao: descricao.unwrap_or_else(|| {
                format!("Ferramenta MCP {nome_original} do servidor {nome_servidor}")
            }),
            nome_original,
            schema_entrada,
            peer,
            timeout,
        }
    }
}

#[async_trait]
impl Tool for McpTool {
    fn name(&self) -> &str {
        &self.nome_completo
    }

    fn description(&self) -> &str {
        &self.descricao
    }

    fn input_schema(&self) -> Value {
        self.schema_entrada.clone()
    }

    async fn execute(&self, context: &ToolContext, input: Value) -> Result<ToolOutput> {
        // GAR-190: audit log — every MCP tool invocation is recorded.
        let input_keys: Vec<&str> = match &input {
            Value::Object(m) => m.keys().map(|k| k.as_str()).collect(),
            _ => vec![],
        };
        info!(
            tool = %self.nome_completo,
            session = %context.session_id,
            input_keys = ?input_keys,
            "mcp tool call"
        );

        // Converte a entrada e injeta a origem da conversa (delegation__*).
        let argumentos = build_mcp_arguments(&self.nome_completo, input, context);

        let mut params = CallToolRequestParams::new(self.nome_original.clone());
        if let Some(a) = argumentos {
            params = params.with_arguments(a);
        }

        // Executa com timeout
        let resultado = tokio::time::timeout(self.timeout, self.peer.call_tool(params))
            .await
            .map_err(|_| {
                Error::Mcp(format!(
                    "ferramenta {} excedeu o tempo limite após {:?}",
                    self.nome_completo, self.timeout
                ))
            })?
            .map_err(|e| Error::Mcp(format!("falha ao chamar call_tool: {e}")))?;

        // Converte conteúdos retornados pelo MCP em texto único
        let mut partes_texto = Vec::new();
        for content in &resultado.content {
            match &content.raw {
                RawContent::Text(text_content) => {
                    partes_texto.push(text_content.text.to_string());
                }
                _ => {
                    // Conteúdo não textual recebe placeholder
                    partes_texto.push("[conteúdo não textual]".to_string());
                }
            }
        }

        let texto_saida = partes_texto.join("\n");
        let eh_erro = resultado.is_error.unwrap_or(false);

        if eh_erro {
            Ok(ToolOutput::error(texto_saida))
        } else {
            Ok(ToolOutput::success(texto_saida))
        }
    }
}

/// Build the MCP `arguments` object from the model's `input`, injecting the
/// authenticated conversation origin for `delegation__*` tools so a delegated
/// task notifies the SAME chat the request came from instead of a hardcoded
/// owner chat. `garra_origin_chat_id` is the authenticated Telegram user id
/// (== chat_id for private chats) and OVERWRITES any model-supplied value so the
/// model cannot spoof the delivery target. Injection is scoped to `delegation__`
/// by name because other MCP servers (e.g. the Node filesystem server) may
/// reject unknown arguments.
fn build_mcp_arguments(
    tool_name: &str,
    input: Value,
    context: &ToolContext,
) -> Option<serde_json::Map<String, Value>> {
    let mut args = match input {
        Value::Object(map) => map,
        Value::Null => serde_json::Map::new(),
        outro => {
            let mut map = serde_json::Map::new();
            map.insert("input".to_string(), outro);
            map
        }
    };
    if tool_name.starts_with("delegation__") {
        if let Some(uid) = context.user_id.as_ref() {
            args.insert(
                "garra_origin_chat_id".to_string(),
                Value::String(uid.clone()),
            );
        }
        args.insert(
            "garra_session_id".to_string(),
            Value::String(context.session_id.clone()),
        );
    }
    if args.is_empty() {
        None
    } else {
        Some(args)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ctx(user: Option<&str>) -> ToolContext {
        ToolContext {
            session_id: "sess-1".into(),
            user_id: user.map(|s| s.to_string()),
            is_heartbeat: false,
            is_confirmation_approved: false,
            working_dir: None,
            project_id: None,
        }
    }

    #[test]
    fn delegation_tool_gets_origin_from_authenticated_user() {
        let a = build_mcp_arguments(
            "delegation__ask_flash",
            json!({"message": "oi"}),
            &ctx(Some("7978617919")),
        )
        .unwrap();
        assert_eq!(a["garra_origin_chat_id"], json!("7978617919"));
        assert_eq!(a["garra_session_id"], json!("sess-1"));
        assert_eq!(a["message"], json!("oi"));
    }

    #[test]
    fn model_cannot_spoof_origin_binary_overwrites() {
        // The model tries to set its own origin; the authenticated id wins.
        let a = build_mcp_arguments(
            "delegation__ask_flash",
            json!({"message": "x", "garra_origin_chat_id": "999"}),
            &ctx(Some("7978617919")),
        )
        .unwrap();
        assert_eq!(a["garra_origin_chat_id"], json!("7978617919"));
    }

    #[test]
    fn no_authenticated_user_injects_no_origin() {
        let a = build_mcp_arguments(
            "delegation__ask_flash",
            json!({"message": "x"}),
            &ctx(None),
        )
        .unwrap();
        assert!(!a.contains_key("garra_origin_chat_id"));
        assert_eq!(a["garra_session_id"], json!("sess-1"));
    }

    #[test]
    fn non_delegation_tool_is_untouched() {
        let a = build_mcp_arguments(
            "filesystem__read_file",
            json!({"path": "/x"}),
            &ctx(Some("7978617919")),
        )
        .unwrap();
        assert!(!a.contains_key("garra_origin_chat_id"));
        assert!(!a.contains_key("garra_session_id"));
        assert_eq!(a["path"], json!("/x"));
    }

    #[test]
    fn null_input_non_delegation_is_none() {
        assert!(build_mcp_arguments("filesystem__list", Value::Null, &ctx(None)).is_none());
    }
}
