//! Folder layout of an installation, the model roles and the router presets written for llama-server.
//!
//!   TypeLogic.exe
//!   runtime/llama/       llama-server(.exe) and its libraries (pinned llama.cpp release, see config/models.lock.json)
//!   models/              GGUF files
//!   config/              models.lock.json (pinned sources), models.ini (generated router presets)
//!   data/                decisions and archives (the application's own data)
//!   logs/                llama-server.log
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

pub const EXE: &str = if cfg!(windows) { ".exe" } else { "" };

#[derive(Debug, Clone)]
pub struct Layout {
    pub root: PathBuf,
}

/// A model the application can serve, by role: one file per role, the one chosen by the phase 0 spikes.
#[derive(Debug, Clone, Copy)]
pub struct Role {
    pub name: &'static str,
    pub files: &'static [&'static str],
    /// llama-server arguments of this model instance (preset keys, without leading dashes)
    pub args: &'static [(&'static str, &'static str)],
    pub load_on_startup: bool,
    /// GPU memory of the loaded model with these arguments (MiB, measured on the B580 by spike/trio.py)
    pub device_mib: u64,
}

// The three models share one 12 GB GPU (spike/trio.py measures the fit). Common to all: the tied output matrix
// (token_embd, n_vocab x n_embd) stays in system memory; the decision models never use it, Plumb for one token.
pub const ROLES: &[Role] = &[
    Role {
        name: "specialist",
        files: &["typelogic-specialist-2b-Q8_0.gguf"],
        // a question with its options must fit in one micro-batch (Kev); needs the patched llama.dll to stay small
        args: &[("ctx-size", "12288"), ("parallel", "2"), ("batch-size", "3072"), ("ubatch-size", "3072"), ("override-tensor", "token_embd.weight=CPU")],
        load_on_startup: true,
        device_mib: 2084,
    },
    Role {
        name: "generalist", // Plumb: direct readout for up to 16 options, and titles
        files: &["plumb-4b-v5-Q4_K_M.gguf"],
        args: &[
            ("ctx-size", "8192"),
            ("parallel", "1"),
            ("batch-size", "2048"),
            ("ubatch-size", "512"),
            ("flash-attn", "on"),
            ("cache-type-k", "q8_0"),
            ("cache-type-v", "q8_0"),
            ("cache-ram", "1024"),
            ("override-tensor", "token_embd.weight=CPU"),
        ],
        load_on_startup: true,
        device_mib: 2378,
    },
    Role {
        name: "clef", // optional download
        // IQ4_XS: beside the specialist and Plumb it leaves ~1.1 GiB of a 12 GB GPU free with the accuracy of Q4_K_M
        // (spike/trio.py); the Q4_K_M files leave 0.5 GiB or do not fit at all
        files: &["Cloudflare_clef-flash-IQ4_XS.gguf"],
        // Clef reads the whole request (state and every question) in one micro-batch
        args: &[("ctx-size", "8192"), ("parallel", "1"), ("batch-size", "4096"), ("ubatch-size", "4096"), ("override-tensor", "token_embd.weight=CPU")],
        load_on_startup: true,
        device_mib: 5831,
    },
];

/// GPU memory left free for the desktop and other programs when choosing the models (MiB).
pub const GPU_MARGIN_MIB: u64 = 512;

/// The models to load and where: decided at every start from the files present and the free GPU memory.
#[derive(Debug, Clone)]
pub struct Plan {
    pub models: Vec<(&'static Role, PathBuf)>,
    /// llama.cpp device (e.g. Vulkan0), or None to run on the CPU
    pub device: Option<String>,
    pub reason: String,
}

/// A GPU reported by `llama-server --list-devices`.
#[derive(Debug, Clone, PartialEq)]
pub struct Gpu {
    pub name: String,
    pub description: String,
    pub free_mib: u64,
}

/// GPUs from the `--list-devices` output; software renderers are skipped.
pub fn parse_devices(text: &str) -> Vec<Gpu> {
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        let Some((name, rest)) = line.split_once(':') else { continue };
        let name = name.trim();
        if name.is_empty() || !name.chars().last().is_some_and(|c| c.is_ascii_digit()) || name.contains(' ') {
            continue;
        }
        // "Intel(R) Arc(TM) B580 Graphics (12190 MiB, 11421 MiB free)"
        let Some(open) = rest.rfind('(') else { continue };
        let numbers: Vec<u64> = rest[open..].split(|c: char| !c.is_ascii_digit()).filter_map(|t| t.parse().ok()).collect();
        let [_, free] = numbers[..] else { continue };
        let description = rest[..open].trim().to_owned();
        let lower = description.to_lowercase();
        if ["llvmpipe", "swiftshader", "basic render", "lavapipe"].iter().any(|s| lower.contains(s)) {
            continue;
        }
        out.push(Gpu { name: name.to_owned(), description, free_mib: free });
    }
    out
}

impl Layout {
    /// The folder of the executable, or TYPELOGIC_HOME when set (development).
    pub fn discover() -> std::io::Result<Self> {
        if let Some(home) = std::env::var_os("TYPELOGIC_HOME") {
            return Ok(Self { root: PathBuf::from(home) });
        }
        let exe = std::env::current_exe()?;
        let root = exe.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from("."));
        Ok(Self { root })
    }

    pub fn llama_dir(&self) -> PathBuf { self.root.join("runtime").join("llama") }
    pub fn llama_server(&self) -> PathBuf { self.llama_dir().join(format!("llama-server{EXE}")) }
    pub fn models_dir(&self) -> PathBuf { self.root.join("models") }
    pub fn config_dir(&self) -> PathBuf { self.root.join("config") }
    pub fn presets(&self) -> PathBuf { self.config_dir().join("models.ini") }
    pub fn logs_dir(&self) -> PathBuf { self.root.join("logs") }

    /// (role, file) for every role that has a model file on disk.
    pub fn available(&self) -> Vec<(&'static Role, PathBuf)> {
        ROLES
            .iter()
            .filter_map(|role| role.files.iter().map(|f| self.models_dir().join(f)).find(|p| p.is_file()).map(|p| (role, p)))
            .collect()
    }

    /// Which models to load. With a GPU: Plumb + Clef-Flash when both fit (Clef answers the catalogs, the specialist
    /// is not needed), otherwise specialist + Plumb; without a GPU, or one too small, both on the CPU.
    /// TYPELOGIC_MODELS=specialist,generalist,clef and TYPELOGIC_DEVICE=cpu|Vulkan0 override the choice.
    /// `cpu_only` is the "CPU only" mode of the app (Engine & privacy).
    pub fn plan(&self, gpus: &[Gpu], cpu_only: bool) -> Plan {
        let available = self.available();
        let wanted = std::env::var("TYPELOGIC_MODELS").ok().filter(|v| !v.trim().is_empty());
        let device_env = std::env::var("TYPELOGIC_DEVICE").ok().filter(|v| !v.trim().is_empty());
        let device = if cpu_only { Some("cpu") } else { device_env.as_deref() };
        let mut plan = choose(&available, gpus, wanted.as_deref(), device);
        if cpu_only && wanted.is_none() {
            plan.reason = "CPU only (Engine & privacy): specialist + Plumb on the CPU".into();
        }
        plan
    }

    /// The router preset file: one section per planned model, shared settings in [*].
    pub fn presets_text(&self, plan: &Plan) -> String {
        let mut out = String::from("; Generated by TypeLogic.exe at every start; edit config/models.lock.json, not this file.\nversion = 1\n\n");
        match &plan.device {
            Some(d) => {
                let _ = write!(out, "[*]\nn-gpu-layers = 999\ndevice = {d}\nno-webui = true\n\n");
            }
            None => out.push_str("[*]\nn-gpu-layers = 0\ndevice = none\nno-webui = true\n\n"),
        }
        // TYPELOGIC_BATCH (diagnostics, small computers): one micro-batch size for every model
        let batch = std::env::var("TYPELOGIC_BATCH").ok().filter(|v| v.parse::<u32>().is_ok_and(|n| (64..=65536).contains(&n)));
        for (role, path) in &plan.models {
            let _ = writeln!(out, "[{}]", role.name);
            let _ = writeln!(out, "model = {}", path.display());
            for (k, v) in role.args {
                if plan.device.is_none() && *k == "override-tensor" {
                    continue; // everything is in system memory already
                }
                let v = match (*k, &batch) {
                    ("batch-size" | "ubatch-size", Some(b)) => b.as_str(),
                    _ => v,
                };
                let _ = writeln!(out, "{k} = {v}");
            }
            let _ = writeln!(out, "load-on-startup = {}\n", role.load_on_startup);
        }
        out
    }
}

/// The plan for the models present and the GPUs found (see Layout::plan).
pub fn choose(available: &[(&'static Role, PathBuf)], gpus: &[Gpu], wanted: Option<&str>, device: Option<&str>) -> Plan {
    let pick = |names: &[&str]| -> Vec<(&'static Role, PathBuf)> { available.iter().filter(|(r, _)| names.contains(&r.name)).cloned().collect() };
    let need = |models: &[(&'static Role, PathBuf)]| models.iter().map(|(r, _)| r.device_mib).sum::<u64>() + GPU_MARGIN_MIB;
    let gpu = match device {
        Some(d) if d.eq_ignore_ascii_case("cpu") || d.eq_ignore_ascii_case("none") => None,
        Some(d) => gpus.iter().find(|g| g.name.eq_ignore_ascii_case(d)).cloned().or(Some(Gpu { name: d.to_owned(), description: String::new(), free_mib: u64::MAX })),
        None => gpus.iter().max_by_key(|g| g.free_mib).cloned(),
    };
    if let Some(list) = wanted {
        let names: Vec<&str> = list.split(',').map(str::trim).collect();
        let models = pick(&names);
        return Plan { device: gpu.map(|g| g.name), reason: format!("TYPELOGIC_MODELS={list}"), models };
    }
    let has = |n: &str| available.iter().any(|(r, _)| r.name == n);
    let fast = pick(&["generalist", "clef"]);
    let base = pick(&["specialist", "generalist"]);
    if let Some(g) = &gpu {
        if has("clef") && has("generalist") && need(&fast) <= g.free_mib {
            return Plan { models: fast, device: Some(g.name.clone()), reason: format!("{} MiB free on {}: Plumb + Clef-Flash", g.free_mib, g.name) };
        }
        if !base.is_empty() && need(&base) <= g.free_mib {
            return Plan { models: base, device: Some(g.name.clone()), reason: format!("{} MiB free on {}: specialist + Plumb", g.free_mib, g.name) };
        }
    }
    let why = match &gpu {
        Some(g) => format!("{} MiB free on {} is not enough: specialist + Plumb on the CPU", g.free_mib, g.name),
        None => "no GPU: specialist + Plumb on the CPU".to_owned(),
    };
    // a computer with only Clef-Flash (no specialist) still gets an answer on the CPU, slowly
    let models = if base.is_empty() { pick(&["clef"]) } else { base };
    Plan { models, device: None, reason: why }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gpu(free: u64) -> Gpu { Gpu { name: "Vulkan0".into(), description: "Intel(R) Arc(TM) B580 Graphics".into(), free_mib: free } }

    fn models(names: &[&str]) -> Vec<(&'static Role, PathBuf)> {
        ROLES.iter().filter(|r| names.contains(&r.name)).map(|r| (r, PathBuf::from(r.files[0]))).collect()
    }

    fn names(plan: &Plan) -> Vec<&str> { plan.models.iter().map(|(r, _)| r.name).collect() }

    #[test]
    fn presets_list_only_present_models() {
        let dir = std::env::temp_dir().join(format!("tl-layout-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("models")).unwrap();
        std::fs::write(dir.join("models").join("typelogic-specialist-2b-Q8_0.gguf"), b"x").unwrap();
        std::fs::write(dir.join("models").join("Clef-Flash-Q4_K_M.gguf"), b"x").unwrap(); // not a v2 file: ignored
        let layout = Layout { root: dir.clone() };
        let plan = choose(&layout.available(), &[gpu(11000)], None, None);
        let text = layout.presets_text(&plan);
        assert!(text.contains("[specialist]") && text.contains("Q8_0") && text.contains("load-on-startup = true"));
        assert!(!text.contains("[generalist]") && !text.contains("[clef]"));
        assert!(text.contains("override-tensor = token_embd.weight=CPU") && text.contains("device = Vulkan0"));
        let cpu = layout.presets_text(&choose(&layout.available(), &[], None, None));
        assert!(cpu.contains("n-gpu-layers = 0") && !cpu.contains("override-tensor"));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn plans_by_free_gpu_memory() {
        let all = models(&["specialist", "generalist", "clef"]);
        assert_eq!(names(&choose(&all, &[gpu(11421)], None, None)), vec!["generalist", "clef"]);
        assert_eq!(names(&choose(&all, &[gpu(7000)], None, None)), vec!["specialist", "generalist"]);
        let small = choose(&all, &[gpu(3000)], None, None);
        assert_eq!((names(&small), small.device.clone()), (vec!["specialist", "generalist"], None));
        let none = choose(&models(&["specialist", "generalist"]), &[], None, None);
        assert!(none.device.is_none() && none.reason.contains("no GPU"));
        assert!(choose(&all, &[gpu(11421)], None, Some("cpu")).device.is_none());
        assert_eq!(names(&choose(&all, &[gpu(11421)], Some("specialist,generalist,clef"), None)).len(), 3);
        assert_eq!(names(&choose(&models(&["clef"]), &[], None, None)), vec!["clef"]);
    }

    #[test]
    fn reads_list_devices() {
        let text = "ggml_vulkan: Found 2 Vulkan devices:\nAvailable devices:\n  Vulkan0: Intel(R) Arc(TM) B580 Graphics (12190 MiB, 11421 MiB free)\n  Vulkan1: llvmpipe (LLVM 15.0.7, 256 bits) (7972 MiB, 7972 MiB free)\n";
        assert_eq!(parse_devices(text), vec![gpu(11421)]);
        assert!(parse_devices("Available devices:\n").is_empty());
    }
}
