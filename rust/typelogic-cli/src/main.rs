//! `typelogic-core`: JSON-lines bridge to the core for any language (for calling the core from any language).
//!
//! Each input line is `{"id": any, "method": "...", "params": {...}}`; each output line is
//! `{"id": any, "ok": ...}` or `{"id": any, "error": {"type", "message"}}`. One request per line,
//! answered in order. `typelogic-core version` and `typelogic-core call METHOD PARAMS_JSON` run once.
use std::io::{self, BufRead, Write};
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};

const MAX_LINE: usize = 16 * 1024 * 1024;

fn respond(id: Value, response_json: &str) -> String {
    let mut out = Map::new();
    out.push_unique("id", id);
    if let Ok(Value::Object(m)) = loads(response_json, true) {
        for (k, v) in m.into_entries() {
            out.push_unique(k, v);
        }
    }
    dumps(&Value::Object(out), DumpOptions { sort_keys: false, compact: true, ensure_ascii: false, allow_nan: false }).unwrap_or_default()
}

fn error_line(id: Value, message: &str) -> String {
    let mut err = Map::new();
    err.push_unique("type", Value::str("ValueError"));
    err.push_unique("message", Value::str(message));
    let mut out = Map::new();
    out.push_unique("id", id);
    out.push_unique("error", Value::Object(err));
    dumps(&Value::Object(out), DumpOptions::COMPACT_SORTED).unwrap_or_default()
}

fn handle(line: &str) -> String {
    if line.len() > MAX_LINE {
        return error_line(Value::Null, "request line exceeds 16 MB");
    }
    let request = match loads(line, true) {
        Ok(Value::Object(m)) => m,
        Ok(_) => return error_line(Value::Null, "request must be a JSON object"),
        Err(e) => return error_line(Value::Null, &e.message),
    };
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let Some(method) = request.get("method").and_then(Value::as_str) else {
        return error_line(id, "request requires a string method");
    };
    let params = request.get("params").cloned().unwrap_or(Value::Object(Map::new()));
    let params_json = dumps(&params, DumpOptions::COMPACT_SORTED).unwrap_or_else(|_| "{}".into());
    respond(id, &typelogic_core::api::call(method, &params_json))
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version") => println!("{}", typelogic_core::VERSION),
        Some("call") if args.len() == 3 => println!("{}", typelogic_core::api::call(&args[1], &args[2])),
        Some("--help") | Some("-h") => println!("usage: typelogic-core [version | call METHOD PARAMS_JSON]  (default: JSON lines on stdin)"),
        Some(other) => {
            eprintln!("unknown argument: {other}");
            std::process::exit(2);
        }
        None => {
            let stdin = io::stdin();
            let mut stdout = io::stdout().lock();
            for line in stdin.lock().lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                if writeln!(stdout, "{}", handle(&line)).and_then(|_| stdout.flush()).is_err() {
                    break;
                }
            }
        }
    }
}
