//! TypeLogic.exe - the single entry point of TypeLogic v2.
//!
//!   TypeLogic [serve] [--port N] [--no-browser] [--data DIR]   the local web UI, the API and the models (default)
//!   TypeLogic setup [--with-clef] [--from DIR] [--verify]  install the runtime and the models (models.lock.json)
//!   TypeLogic mcp [--port N]                           MCP server on stdio (tool: decide)
//!   TypeLogic import FILE [--data DIR]                 add the decisions of a 1.x export (typelogic-export.json)
//!   TypeLogic doctor                                   runtime, models, GPUs and the models this computer will load
//!   TypeLogic version
//!
//! Everything listens on 127.0.0.1 only; nothing is downloaded at run time.
mod app;
mod archive;
mod bench;
mod casefmt;
mod direct;
mod engine;
mod evidence;
mod http;
#[cfg(windows)]
mod job;
mod layout;
mod library;
mod mcp;
mod needle;
mod router;
mod setup;
mod templates;
mod titles;
mod util;
mod web;

use std::net::TcpListener;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_PORT: u16 = 8765;

struct Options {
    command: String,
    port: u16,
    browser: bool,
    setup: setup::Options,
    /// data folder (default: <installation>/data)
    data: Option<std::path::PathBuf>,
    /// the deterministic engine of the UI tests (builds with the `fixture` feature only)
    fixture: bool,
    /// import: the export file
    file: Option<std::path::PathBuf>,
}

fn parse(args: &[String]) -> Result<Options, String> {
    let mut o = Options { command: "serve".into(), port: DEFAULT_PORT, browser: true, setup: setup::Options::default(), data: None, fixture: false, file: None };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "serve" | "setup" | "mcp" | "doctor" | "version" | "help" => o.command = a.clone(),
            "import" => {
                o.command = a.clone();
                o.file = Some(it.next().ok_or("import needs the export file (typelogic-export.json)")?.into());
            }
            "--with-clef" => o.setup.with_clef = true,
            "--verify" => o.setup.verify = true,
            "--from" => o.setup.from.push(it.next().ok_or("--from needs a folder")?.into()),
            "--help" | "-h" => o.command = "help".into(),
            "--version" | "-V" => o.command = "version".into(),
            "--no-browser" => o.browser = false,
            "--data" => o.data = Some(it.next().ok_or("--data needs a folder")?.into()),
            "--fixture" if cfg!(feature = "fixture") => o.fixture = true,
            "--port" => {
                let v = it.next().ok_or("--port needs a value")?;
                o.port = v.parse().map_err(|_| format!("invalid port: {v}"))?;
            }
            other => return Err(format!("unknown argument: {other} (try: TypeLogic help)")),
        }
    }
    Ok(o)
}

fn open_browser(url: &str) {
    #[cfg(windows)]
    let _ = std::process::Command::new("rundll32").args(["url.dll,FileProtocolHandler", url]).spawn();
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(url).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
}

/// Start the inference server with the models chosen for this computer (layout::Plan), and wait for them.
/// `cpu_only` is the "CPU only" mode of Engine & privacy.
pub(crate) fn start_inference(layout: &layout::Layout, cpu_only: bool) -> app::Inference {
    let mut inf = app::Inference::empty();
    if layout.available().is_empty() {
        inf.status = "error";
        inf.error = Some(format!("No model in {}; run: TypeLogic setup", layout.models_dir().display()));
        eprintln!("{}", inf.error.as_deref().unwrap_or(""));
        return inf;
    }
    let (gpus, _) = router::gpus(layout);
    let plan = layout.plan(&gpus, cpu_only);
    let names: Vec<&str> = plan.models.iter().map(|(r, _)| r.name).collect();
    eprintln!("Starting the inference server: {} ({})...", names.join(" + "), plan.reason);
    inf.plan = plan.reason.clone();
    inf.device = plan.device.clone();
    inf.models = plan.models.iter().map(|(r, p)| (r.name.to_owned(), p.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default())).collect();
    let started = router::Router::start(layout, &plan).map_err(|e| e.to_string()).and_then(|mut r| {
        let failed = r.wait_ready(&names, Duration::from_secs(900)).map_err(|e| e.to_string())?;
        Ok((r, failed))
    });
    let (r, failed) = match started {
        Ok(x) => x,
        Err(e) => {
            inf.status = "error";
            inf.error = Some(e);
            return inf;
        }
    };
    for f in &failed {
        eprintln!("warning: the {f} model did not load; see logs/llama-server.log");
    }
    let loaded: Vec<engine::Engine> = names.iter().filter(|n| !failed.iter().any(|f| f == *n)).filter_map(|n| engine::Engine::parse(n)).collect();
    let ctx = plan
        .models
        .iter()
        .find(|(r, _)| r.name == "generalist")
        .and_then(|(r, _)| r.args.iter().find(|(k, _)| *k == "ctx-size"))
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(8192);
    if loaded.is_empty() {
        inf.status = "error";
        inf.error = Some("no model loaded; see logs/llama-server.log".into());
    } else {
        inf.engines = Some(Arc::new(engine::Engines::new(r.addr, loaded, ctx)));
        inf.status = "ready";
    }
    inf.router = Some(r);
    inf
}

/// Whether this process has a console of its own (Windows: started by a double-click, not from a terminal).
fn own_console() -> bool {
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        extern "system" {
            fn GetConsoleProcessList(list: *mut u32, count: u32) -> u32;
        }
        let mut ids = [0u32; 4];
        // 1: only this process is attached to the console, so it was opened for it
        unsafe { GetConsoleProcessList(ids.as_mut_ptr(), ids.len() as u32) == 1 }
    }
    #[cfg(not(windows))]
    false
}

fn ask(question: &str, default_yes: bool) -> bool {
    use std::io::Write;
    print!("{question} [{}] ", if default_yes { "Y/n" } else { "y/N" });
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().read_line(&mut line).is_err() {
        return false;
    }
    match line.trim().to_lowercase().as_str() {
        "" => default_yes,
        a => a.starts_with('y') || a.starts_with('s'),
    }
}

/// First start of a fresh installation: offer to install what is missing (the runtime and the default models of
/// models.lock.json, Clef-Flash when the GPU has room) before the web interface opens. Only when someone can answer
/// (an interactive console); otherwise the interface opens and shows how to run setup.
fn first_run(layout: &layout::Layout) -> Result<(), String> {
    use std::io::IsTerminal;
    let lock = std::fs::read_to_string(layout.config_dir().join("models.lock.json"))
        .ok()
        .and_then(|t| typelogic_core::json::loads(&t, false).ok())
        .ok_or_else(|| format!("{} is missing; extract the whole TypeLogic archive", layout.config_dir().join("models.lock.json").display()))?;
    let wanted = setup::models_to_install(&lock, false)?;
    let missing: Vec<&setup::Item> = wanted.iter().filter(|it| !layout.models_dir().join(&it.file).is_file()).collect();
    let runtime = layout.llama_server().is_file();
    if runtime && missing.is_empty() {
        return Ok(());
    }
    if !std::io::stdin().is_terminal() {
        eprintln!("Models or runtime missing; run: TypeLogic setup");
        return Ok(());
    }
    let gib = |b: u64| b as f64 / (1u64 << 30) as f64;
    println!("TypeLogic {VERSION}: first start in {}", layout.root.display());
    if !runtime {
        println!("  - llama.cpp runtime (about 36 MB)");
    }
    for it in &missing {
        println!("  - {} ({:.2} GiB)", it.file, gib(it.bytes));
    }
    let total: u64 = missing.iter().map(|it| it.bytes).sum();
    println!("Files come from their publishers (GitHub, Hugging Face) and are checked against config/models.lock.json.");
    if !ask(&format!("Download about {:.1} GiB now?", gib(total)), true) {
        println!("Nothing downloaded. Run 'TypeLogic setup' later; the interface opens without models.");
        return Ok(());
    }
    let clef = setup::models_to_install(&lock, true)?.into_iter().find(|it| !wanted.contains(it) && !layout.models_dir().join(&it.file).is_file());
    let mut with_clef = false;
    if let Some(c) = clef {
        let roomy = runtime && {
            let (gpus, _) = router::gpus(layout);
            let need: u64 = layout::ROLES.iter().filter(|r| r.name == "generalist" || r.name == "clef").map(|r| r.device_mib).sum::<u64>() + layout::GPU_MARGIN_MIB;
            gpus.iter().any(|g| g.free_mib >= need)
        };
        with_clef = ask(
            &format!(
                "Also download Clef-Flash ({:.1} GiB), the most accurate model for large catalogs?{}",
                gib(c.bytes),
                if roomy { " This GPU has room for it." } else { " It needs a GPU with about 9 GB free." }
            ),
            roomy,
        );
    }
    setup::run(layout, &setup::Options { with_clef, ..Default::default() })
}

fn serve(o: &Options) -> Result<(), String> {
    let layout = layout::Layout::discover().map_err(|e| e.to_string())?;
    if !o.fixture {
        first_run(&layout)?;
    }
    let listener = TcpListener::bind(("127.0.0.1", o.port)).map_err(|e| format!("port {} is busy ({e}); use --port", o.port))?;
    let data = o.data.clone().unwrap_or_else(|| layout.root.join("data"));
    let starter: Box<app::Starter> = if o.fixture { fixture_starter() } else { Box::new(start_inference) };
    let app = Arc::new(app::App::new(layout, data, VERSION, starter)?);
    app.load();
    let url = format!("http://127.0.0.1:{}", listener.local_addr().map(|a| a.port()).unwrap_or(o.port));
    println!("{url}"); // first line of stdout: the address (read by the UI tests and launchers)
    eprintln!("TypeLogic {VERSION}: {url}  (Ctrl+C to stop)");
    if o.browser {
        open_browser(&url);
    }
    let handler: Arc<http::Handler> = Arc::new(move |request| app.handle(request));
    http::serve(listener, handler).map_err(|e| e.to_string())
}

#[cfg(feature = "fixture")]
fn fixture_starter() -> Box<app::Starter> {
    Box::new(|_, _| {
        let mut inf = app::Inference::empty();
        inf.engines = Some(Arc::new(app::Fixture));
        inf.status = "ready";
        inf.plan = "fixture engine (UI tests)".into();
        inf.models = vec![("specialist".into(), "fixture".into()), ("generalist".into(), "fixture".into())];
        inf
    })
}

#[cfg(not(feature = "fixture"))]
fn fixture_starter() -> Box<app::Starter> { Box::new(start_inference) }

/// Add the decisions of a 1.x export to the library (verified records only; existing ones are kept).
fn import(o: &Options) -> Result<(), String> {
    let layout = layout::Layout::discover().map_err(|e| e.to_string())?;
    let data = o.data.clone().unwrap_or_else(|| layout.root.join("data"));
    let file = o.file.as_ref().ok_or("import needs the export file")?;
    let text = std::fs::read_to_string(file).map_err(|e| format!("{}: {e}", file.display()))?;
    let export = typelogic_core::json::loads(&text, false).map_err(|e| format!("{}: {}", file.display(), e.message))?;
    let mut lib = library::Library::open(&data.join("library")).map_err(|e| e.to_string())?;
    let r = lib.import(&export, archive::limit())?;
    println!("{}", typelogic_core::json::dumps(&r, typelogic_core::json::DumpOptions::REPLY).map_err(|e| e.message)?);
    Ok(())
}

fn doctor() -> Result<(), String> {
    let layout = layout::Layout::discover().map_err(|e| e.to_string())?;
    println!("TypeLogic {VERSION} (core {})", typelogic_core::VERSION);
    println!("folder      {}", layout.root.display());
    let server = layout.llama_server();
    let runtime = std::fs::read_to_string(layout.llama_dir().join("llama-runtime.json")).unwrap_or_default();
    let field = |k: &str| typelogic_core::json::loads(&runtime, false).ok().and_then(|v| v.as_object().and_then(|m| m.get(k)).and_then(|x| x.as_str().map(str::to_owned)));
    // is the llama.dll in place the patched one of models.lock.json?
    let patched = std::fs::read_to_string(layout.config_dir().join("models.lock.json"))
        .ok()
        .and_then(|t| typelogic_core::json::loads(&t, false).ok())
        .and_then(|v| {
            let m = v.as_object()?.get("runtime")?.as_object()?.get("llama-dll-patched")?.as_object()?;
            m.get("sha256")?.as_str().map(str::to_owned)
        })
        .filter(|_| layout.llama_dir().join("llama.dll").is_file())
        .map(|sha| setup::sha256_file(&layout.llama_dir().join("llama.dll")).is_ok_and(|h| h == sha));
    println!(
        "llama.cpp   {}",
        if !server.is_file() {
            "MISSING (run: TypeLogic setup)".to_owned()
        } else {
            format!(
                "{} (build {}){}",
                field("tag").unwrap_or_else(|| "present".into()),
                field("binaries_tag").unwrap_or_else(|| "?".into()),
                match patched { Some(true) => ", patched llama.dll", Some(false) => ", release llama.dll (the specialist needs about twice the GPU memory)", None => "" }
            )
        }
    );
    for role in layout::ROLES {
        let found = role.files.iter().map(|f| layout.models_dir().join(f)).find(|p| p.is_file());
        match found {
            Some(p) => {
                let gib = std::fs::metadata(&p).map(|m| m.len() as f64 / (1u64 << 30) as f64).unwrap_or(0.0);
                println!("{:<11} {} ({gib:.2} GiB)", role.name, p.file_name().unwrap_or_default().to_string_lossy());
            }
            None => println!("{:<11} not installed", role.name),
        }
    }
    let needle = needle::Needle::discover(&layout.models_dir(), &layout.config_dir());
    println!(
        "{:<11} {}",
        "needle",
        if needle.available() { format!("installed in {} (hashes checked on first use)", needle.dir.display()) } else { "not installed (run: TypeLogic setup)".into() }
    );
    if server.is_file() {
        let (gpus, raw) = router::gpus(&layout);
        println!("devices:");
        for line in raw.lines().filter(|l| !l.trim().is_empty()) {
            println!("  {line}");
        }
        let cpu = app::Settings::load(&layout.root.join("data")).mode == "cpu";
        let plan = layout.plan(&gpus, cpu);
        let names: Vec<&str> = plan.models.iter().map(|(r, _)| r.name).collect();
        println!("plan        {} ({})", if names.is_empty() { "no model".to_owned() } else { names.join(" + ") }, plan.reason);
    }
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match parse(&args) {
        Err(e) => Err(e),
        Ok(o) => match o.command.as_str() {
            "version" => {
                println!("TypeLogic {VERSION} (core {})", typelogic_core::VERSION);
                Ok(())
            }
            "help" => {
                println!("{}", include_str!("main.rs").lines().skip(2).take_while(|l| l.trim() != "//!").map(|l| l.trim_start_matches("//!")).collect::<Vec<_>>().join("\n"));
                Ok(())
            }
            "doctor" => doctor(),
            "setup" => layout::Layout::discover().map_err(|e| e.to_string()).and_then(|l| setup::run(&l, &o.setup)),
            "mcp" => layout::Layout::discover().map_err(|e| e.to_string()).and_then(|l| mcp::run(l, o.port)),
            "import" => import(&o),
            _ => serve(&o),
        },
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("TypeLogic: {e}");
            if own_console() {
                // started by a double-click: keep the window open so the message can be read
                eprintln!("Press Enter to close.");
                let _ = std::io::stdin().read_line(&mut String::new());
            }
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_arguments() {
        let o = parse(&["--port".into(), "9000".into(), "--no-browser".into()]).unwrap();
        assert_eq!((o.command.as_str(), o.port, o.browser), ("serve", 9000, false));
        assert_eq!(parse(&["doctor".into()]).unwrap().command, "doctor");
        assert!(parse(&["--port".into()]).is_err() && parse(&["--bogus".into()]).is_err());
        let s = parse(&["setup".into(), "--with-clef".into(), "--from".into(), "a".into(), "--from".into(), "b".into()]).unwrap();
        assert!(s.command == "setup" && s.setup.with_clef && s.setup.from.len() == 2);
    }
}
