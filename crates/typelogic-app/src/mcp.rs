//! `TypeLogic mcp`: a Model Context Protocol server on stdio (JSON-RPC 2.0, one message per line) with one tool,
//! `decide`, so other agents can ask TypeLogic for typed decisions.
//!
//! When a TypeLogic is already running on the port (the desktop application), the tool calls its /v1/decide;
//! otherwise this process starts the inference server itself on the first call. stdout carries only protocol
//! messages; diagnostics go to stderr.
use crate::engine::Engine;
use crate::http;
use crate::layout::Layout;
use std::io::{BufRead, Write};
use std::net::SocketAddr;
use std::time::Duration;
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};

const PROTOCOL: &str = "2025-06-18";

const DECIDE_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {
    "state": {"description": "The evidence to decide on: text (up to 120,000 characters) or a JSON object/array.", "type": ["string", "object", "array"]},
    "questions": {
      "type": "object",
      "description": "1-32 questions keyed by id. type noul: a yes/no question (criteria may describe \"true\"/\"false\"); choice: one of 2-255 alternatives (criteria: a list of names or an object name -> description); score: an ordered scale (criteria: 2-32 levels, low to high).",
      "additionalProperties": {
        "type": "object",
        "properties": {
          "type": {"enum": ["noul", "choice", "score"]},
          "instructions": {"type": "string"},
          "criteria": {"type": ["array", "object"]},
          "abstain_option": {"type": "string"}
        },
        "required": ["type", "instructions"]
      }
    },
    "threshold": {"type": "number", "minimum": 0.5, "maximum": 1, "description": "Top probability below which an answer is marked for review (default 0.85)."},
    "permutation_check": {"type": "boolean", "description": "Read choice questions again with the options reordered and flag order-sensitive answers (default true)."},
    "engine": {"enum": ["auto", "generalist", "specialist", "clef"], "description": "Force one model (default auto)."}
  },
  "required": ["state", "questions"]
}"#;

const DECIDE_DESCRIPTION: &str = "Typed decision with probabilities from local models (TypeLogic, offline). Returns, per question, \
the probability of every option, the chosen option (choice), P(yes) (noul) or the expected level (score), and review reasons. \
Every answer requires human review; probabilities are not correctness guarantees.";

enum Backend {
    /// a running TypeLogic application
    Remote(SocketAddr),
    /// this process's own inference server (kept so that its process lives as long as this one)
    Local(crate::app::Inference),
    Unavailable(String),
}

struct Server {
    layout: Layout,
    port: u16,
    backend: Option<Backend>,
}

fn rpc_result(id: &Value, result: Value) -> Value {
    let mut m = Map::new();
    m.push_unique("jsonrpc", Value::str("2.0"));
    m.push_unique("id", id.clone());
    m.push_unique("result", result);
    Value::Object(m)
}

fn rpc_error(id: &Value, code: i64, message: &str) -> Value {
    let mut e = Map::new();
    e.push_unique("code", Value::int(code));
    e.push_unique("message", Value::str(message));
    let mut m = Map::new();
    m.push_unique("jsonrpc", Value::str("2.0"));
    m.push_unique("id", id.clone());
    m.push_unique("error", Value::Object(e));
    Value::Object(m)
}

fn tool_result(text: String, is_error: bool) -> Value {
    let mut c = Map::new();
    c.push_unique("type", Value::str("text"));
    c.push_unique("text", Value::Str(text));
    let mut m = Map::new();
    m.push_unique("content", Value::Array(vec![Value::Object(c)]));
    m.push_unique("isError", Value::Bool(is_error));
    Value::Object(m)
}

/// A TypeLogic application answering on the port, if any.
fn running_app(port: u16) -> Option<SocketAddr> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let (status, body) = http::call(addr, "GET", "/api/health", None, Duration::from_secs(3)).ok()?;
    let v = loads(&String::from_utf8_lossy(&body), false).ok()?;
    let ok = status == 200 && v.as_object()?.get("app")?.as_str()? == "TypeLogic" && v.as_object()?.get("inference")?.as_str()? == "ready";
    ok.then_some(addr)
}

impl Server {
    fn backend(&mut self) -> &Backend {
        if self.backend.is_none() {
            self.backend = Some(match running_app(self.port) {
                Some(addr) => {
                    eprintln!("TypeLogic MCP: using the application on {addr}");
                    Backend::Remote(addr)
                }
                None => {
                    let cpu = crate::app::Settings::load(&self.layout.root.join("data")).mode == "cpu";
                    let inf = crate::start_inference(&self.layout, cpu);
                    match inf.status {
                        "ready" => Backend::Local(inf),
                        _ => Backend::Unavailable(inf.error.unwrap_or_else(|| "the models did not load".into())),
                    }
                }
            });
        }
        self.backend.as_ref().expect("set above")
    }

    fn decide(&mut self, args: &Value) -> Value {
        let Some(m) = args.as_object() else { return tool_result("arguments must be an object".into(), true) };
        let engine = m.get("engine").and_then(Value::as_str).unwrap_or("auto").to_owned();
        let mut payload = Map::new();
        for (k, v) in m.iter().filter(|(k, _)| k.as_str() != "engine") {
            payload.push_unique(k.clone(), v.clone());
        }
        let payload = Value::Object(payload);
        let force = match engine.as_str() {
            "auto" | "" => None,
            e => match Engine::parse(e) {
                Some(e) => Some(e),
                None => return tool_result("engine must be auto, generalist, specialist or clef".into(), true),
            },
        };
        let outcome: Result<Value, String> = match self.backend() {
            Backend::Remote(addr) => {
                let body = dumps(&payload, DumpOptions::REPLY).unwrap_or_default();
                let path = match force {
                    Some(e) => format!("/v1/decide?engine={}", e.name()),
                    None => "/v1/decide".into(),
                };
                match http::call(*addr, "POST", &path, Some(body.as_bytes()), Duration::from_secs(1800)) {
                    Ok((200, data)) => loads(&String::from_utf8_lossy(&data), false).map_err(|e| e.message),
                    Ok((status, data)) => Err(format!("TypeLogic ({status}): {}", String::from_utf8_lossy(&data))),
                    Err(e) => Err(format!("TypeLogic application: {e}")),
                }
            }
            Backend::Local(inf) => match &inf.engines {
                Some(e) => e.evaluate(&payload, force, false).map_err(|e| e.message),
                None => Err("the models did not load".into()),
            },
            Backend::Unavailable(why) => Err(why.clone()),
        };
        match outcome.and_then(|v| dumps(&v, DumpOptions::REPLY).map_err(|e| e.message)) {
            Ok(text) => tool_result(text, false),
            Err(e) => tool_result(e, true),
        }
    }

    /// One JSON-RPC message in, an optional reply out (notifications get none).
    fn handle(&mut self, message: &Value) -> Option<Value> {
        let m = message.as_object()?;
        let method = m.get("method").and_then(Value::as_str).unwrap_or("");
        let id = m.get("id").cloned()?; // no id: a notification (e.g. notifications/initialized), no reply
        let params = m.get("params").cloned().unwrap_or(Value::Object(Map::new()));
        Some(match method {
            "initialize" => {
                let version = params.as_object().and_then(|p| p.get("protocolVersion")).and_then(Value::as_str).unwrap_or(PROTOCOL).to_owned();
                let mut caps = Map::new();
                caps.push_unique("tools", Value::Object(Map::new()));
                let mut info = Map::new();
                info.push_unique("name", Value::str("typelogic"));
                info.push_unique("version", Value::str(env!("CARGO_PKG_VERSION")));
                let mut r = Map::new();
                r.push_unique("protocolVersion", Value::Str(version));
                r.push_unique("capabilities", Value::Object(caps));
                r.push_unique("serverInfo", Value::Object(info));
                r.push_unique("instructions", Value::str("Call decide for typed decisions (yes/no, choice, score) with probabilities. Answers are recommendations that require human review."));
                rpc_result(&id, Value::Object(r))
            }
            "ping" => rpc_result(&id, Value::Object(Map::new())),
            "tools/list" => {
                let mut tool = Map::new();
                tool.push_unique("name", Value::str("decide"));
                tool.push_unique("description", Value::str(DECIDE_DESCRIPTION));
                tool.push_unique("inputSchema", loads(DECIDE_SCHEMA, false).expect("valid schema"));
                let mut r = Map::new();
                r.push_unique("tools", Value::Array(vec![Value::Object(tool)]));
                rpc_result(&id, Value::Object(r))
            }
            "tools/call" => {
                let p = params.as_object();
                match p.and_then(|p| p.get("name")).and_then(Value::as_str) {
                    Some("decide") => {
                        let args = p.and_then(|p| p.get("arguments")).cloned().unwrap_or(Value::Null);
                        rpc_result(&id, self.decide(&args))
                    }
                    Some(other) => rpc_error(&id, -32602, &format!("unknown tool: {other}")),
                    None => rpc_error(&id, -32602, "tools/call needs a tool name"),
                }
            }
            "" => rpc_error(&id, -32600, "invalid request"),
            other => rpc_error(&id, -32601, &format!("method not found: {other}")),
        })
    }
}

pub fn run(layout: Layout, port: u16) -> Result<(), String> {
    let mut server = Server { layout, port, backend: None };
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        if line.trim().is_empty() {
            continue;
        }
        let reply = match loads(&line, false) {
            Ok(v) => server.handle(&v),
            Err(e) => Some(rpc_error(&Value::Null, -32700, &format!("parse error: {}", e.message))),
        };
        if let Some(r) = reply {
            let text = dumps(&r, DumpOptions { sort_keys: false, compact: true, ensure_ascii: false, allow_nan: false }).map_err(|e| e.message)?;
            writeln!(stdout, "{text}").and_then(|_| stdout.flush()).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn server() -> Server { Server { layout: Layout { root: std::env::temp_dir().join("tl-mcp-none") }, port: 1, backend: Some(Backend::Unavailable("no model".into())) } }

    fn call(s: &mut Server, json: &str) -> String { dumps(&s.handle(&loads(json, false).unwrap()).unwrap(), DumpOptions::COMPACT_SORTED).unwrap() }

    #[test]
    fn speaks_mcp() {
        let mut s = server();
        let init = call(&mut s, r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}"#);
        assert!(init.contains(r#""protocolVersion":"2025-03-26""#) && init.contains(r#""tools":{}"#), "{init}");
        assert!(s.handle(&loads(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#, false).unwrap()).is_none());
        let tools = call(&mut s, r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#);
        assert!(tools.contains(r#""name":"decide""#) && tools.contains(r#""required":["state","questions"]"#));
        let r = call(&mut s, r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"decide","arguments":{"state":"x","questions":{}}}}"#);
        assert!(r.contains(r#""isError":true"#) && r.contains("no model"), "{r}");
        assert!(call(&mut s, r#"{"jsonrpc":"2.0","id":4,"method":"nope"}"#).contains("-32601"));
    }
}
