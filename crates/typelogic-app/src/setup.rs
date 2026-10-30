//! `TypeLogic setup`: installs the pinned llama.cpp runtime and the models of config/models.lock.json.
//!
//! Every file is checked against its SHA-256 (computed here, typelogic-core) before it is used. Files come from a
//! local folder given with `--from` (hard-linked when possible, so a development copy costs no disk space) or are
//! downloaded with the system's `curl` and unpacked with its `tar` (both ship with Windows 10 and later): the
//! application itself carries no network code, and the run-time process never leaves the loopback address.
//!
//!   TypeLogic setup [--with-clef] [--from DIR]... [--verify]
use crate::layout::{Layout, EXE};
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::Command;
use typelogic_core::hash::{hex, Sha256};
use typelogic_core::json::{loads, Map, Value};

#[derive(Debug, Default)]
pub struct Options {
    pub with_clef: bool,
    pub from: Vec<PathBuf>,
    /// check the SHA-256 of files already installed too (slow for large models)
    pub verify: bool,
}

/// One file to install.
#[derive(Debug, Clone, PartialEq)]
pub struct Item {
    pub key: String,
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
    /// download URL, None when the file is not published (only `--from` can provide it)
    pub url: Option<String>,
}

pub fn sha256_file(path: &Path) -> io::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex(&h.finish()))
}

fn s<'a>(m: &'a Map, k: &str) -> Option<&'a str> { m.get(k).and_then(Value::as_str) }

fn item(key: &str, m: &Map) -> Result<Item, String> {
    let file = s(m, "file").ok_or_else(|| format!("{key}: no file in models.lock.json"))?.to_owned();
    let bytes = m.get("bytes").and_then(|v| v.to_f64().ok()).ok_or_else(|| format!("{key}: no size in models.lock.json"))? as u64;
    let sha256 = s(m, "sha256").ok_or_else(|| format!("{key}: no sha256 in models.lock.json"))?.to_lowercase();
    let url = match (s(m, "url"), s(m, "repo"), s(m, "revision")) {
        (Some(u), _, _) => Some(u.to_owned()),
        (None, Some(repo), Some(rev)) => Some(format!("https://huggingface.co/{repo}/resolve/{rev}/{file}")),
        _ => None,
    };
    Ok(Item { key: key.to_owned(), file, bytes, sha256, url })
}

/// The models to install: every `default` entry, plus the `optional` ones with `--with-clef`.
pub fn models_to_install(lock: &Value, with_clef: bool) -> Result<Vec<Item>, String> {
    let mut out = Vec::new();
    for section in ["models", "built"] {
        let Some(entries) = lock.as_object().and_then(|m| m.get(section)).and_then(Value::as_object) else { continue };
        for (key, v) in entries.iter() {
            let Some(m) = v.as_object() else { continue };
            match s(m, "v2") {
                Some("default") => out.push(item(key, m)?),
                Some("optional") if with_clef => out.push(item(key, m)?),
                _ => {}
            }
        }
    }
    Ok(out)
}

/// A file with the expected size (and hash when `hash` is set).
fn matches(path: &Path, it: &Item, hash: bool) -> bool {
    let Ok(meta) = fs::metadata(path) else { return false };
    meta.is_file() && (it.bytes == 0 || meta.len() == it.bytes) && (!hash || sha256_file(path).is_ok_and(|h| h == it.sha256))
}

/// Hard link (same volume) or copy `src` to `dest`.
fn place(src: &Path, dest: &Path) -> io::Result<&'static str> {
    if let Some(dir) = dest.parent() {
        fs::create_dir_all(dir)?;
    }
    let _ = fs::remove_file(dest);
    if fs::hard_link(src, dest).is_ok() {
        return Ok("linked");
    }
    fs::copy(src, dest)?;
    Ok("copied")
}

/// Download with the system curl (resumes a partial file), then check size and hash.
fn download(url: &str, dest: &Path, it: &Item) -> Result<(), String> {
    if let Some(dir) = dest.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let part = dest.with_extension(format!("{}.part", dest.extension().and_then(|e| e.to_str()).unwrap_or("")));
    if it.bytes > 0 && fs::metadata(&part).is_ok_and(|m| m.len() > it.bytes) {
        let _ = fs::remove_file(&part);
    }
    match it.bytes {
        0 => println!("  downloading {} from {url}", it.file),
        b if b < 1 << 30 => println!("  downloading {} ({:.1} MiB) from {url}", it.file, b as f64 / (1u64 << 20) as f64),
        b => println!("  downloading {} ({:.2} GiB) from {url}", it.file, b as f64 / (1u64 << 30) as f64),
    }
    let status = Command::new(if cfg!(windows) { "curl.exe" } else { "curl" })
        .args(["-L", "--fail", "--retry", "3", "-C", "-", "--progress-bar", "-o"])
        .arg(&part)
        .arg(url)
        .status()
        .map_err(|e| format!("curl could not start ({e}); download {url} to {} by hand", dest.display()))?;
    if !status.success() {
        return Err(format!("{}: download failed ({status}); run setup again to resume", it.file));
    }
    if !matches(&part, it, true) {
        let _ = fs::remove_file(&part);
        return Err(format!("{}: size or SHA-256 differs from models.lock.json; the file was removed", it.file));
    }
    fs::rename(&part, dest).map_err(|e| e.to_string())
}

/// Install one file at `dest`: already there, from a `--from` folder, or downloaded.
fn install(it: &Item, dest: &Path, o: &Options) -> Result<String, String> {
    if matches(dest, it, o.verify) {
        return Ok("present".into());
    }
    for dir in &o.from {
        let src = dir.join(&it.file);
        if src != dest && matches(&src, it, true) {
            let how = place(&src, dest).map_err(|e| format!("{}: {e}", dest.display()))?;
            return Ok(format!("{how} from {}", dir.display()));
        }
    }
    match &it.url {
        Some(url) => download(url, dest, it).map(|_| "downloaded".into()),
        None => Err(format!("{} is not published yet; build it (see docs/SPIKES.md) and run: TypeLogic setup --from <folder with {}>", it.file, it.file)),
    }
}

/// The release archive of llama.cpp unpacked into runtime/llama: llama-server, its libraries and licenses.
fn install_runtime(layout: &Layout, lock: &Map, o: &Options) -> Result<String, String> {
    let info = lock.get("llama.cpp").and_then(Value::as_object).ok_or("models.lock.json has no runtime.llama.cpp")?;
    let sha = s(info, "sha256").unwrap_or("");
    let marker = layout.llama_dir().join("llama-runtime.json");
    if layout.llama_server().is_file() && fs::read_to_string(&marker).is_ok_and(|t| t.contains(sha)) {
        return Ok(format!("llama.cpp {} present", s(info, "tag").unwrap_or("?")));
    }
    if !cfg!(windows) {
        return Err(format!("the pinned llama.cpp build is for Windows; put a llama.cpp {} build in {}", s(info, "tag").unwrap_or("?"), layout.llama_dir().display()));
    }
    let asset = s(info, "asset").ok_or("runtime.llama.cpp.asset missing")?;
    let it = Item {
        key: "llama.cpp".into(),
        file: asset.into(),
        bytes: info.get("bytes").and_then(|v| v.to_f64().ok()).unwrap_or(0.0) as u64,
        sha256: sha.into(),
        url: Some(format!("https://github.com/{}/releases/download/{}/{asset}", s(info, "repo").unwrap_or("ggml-org/llama.cpp"), s(info, "binaries_tag").unwrap_or(""))),
    };
    let downloads = layout.root.join("runtime").join("downloads");
    let mut from = o.from.clone();
    from.push(downloads.clone());
    let zip = downloads.join(asset);
    let how = install(&it, &zip, &Options { from, ..Default::default() })?;
    let tmp = layout.root.join("runtime").join("llama.unpack");
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let status = Command::new("tar.exe").arg("-xf").arg(&zip).arg("-C").arg(&tmp).status().map_err(|e| format!("tar could not start: {e}"))?;
    if !status.success() {
        return Err(format!("{asset}: could not unpack ({status})"));
    }
    let target = layout.llama_dir();
    let _ = fs::remove_dir_all(&target);
    fs::create_dir_all(&target).map_err(|e| e.to_string())?;
    // flat copy of what llama-server needs; the other tools of the archive are left out
    let mut stack = vec![tmp.clone()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).map_err(|e| e.to_string())?.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            if path.is_dir() {
                stack.push(path);
            } else if name.ends_with(".dll") || name == format!("llama-server{EXE}") || name.starts_with("LICENSE") {
                fs::rename(&path, target.join(&name)).or_else(|_| fs::copy(&path, target.join(&name)).map(|_| ())).map_err(|e| e.to_string())?;
            }
        }
    }
    let _ = fs::remove_dir_all(&tmp);
    if !layout.llama_server().is_file() {
        return Err(format!("llama-server{EXE} not found in {asset}"));
    }
    let mut m = Map::new();
    for k in ["tag", "binaries_tag", "commit", "asset", "sha256"] {
        m.push_unique(k, Value::str(s(info, k).unwrap_or("")));
    }
    fs::write(&marker, typelogic_core::json::dumps(&Value::Object(m), typelogic_core::json::DumpOptions::CANONICAL).unwrap_or_default()).map_err(|e| e.to_string())?;
    Ok(format!("llama.cpp {} installed ({how})", s(info, "tag").unwrap_or("?")))
}

/// The patched llama.dll over the release one (the release DLL is kept as llama.dll.release).
fn install_patch(layout: &Layout, lock: &Map, o: &Options) -> Result<String, String> {
    let Some(info) = lock.get("llama-dll-patched").and_then(Value::as_object) else { return Ok("no patch listed".into()) };
    if !cfg!(windows) {
        return Ok("patch: Windows only (build llama.cpp with spike/patches/qwen35-skip-lm-head.patch elsewhere)".into());
    }
    let it = item("llama-dll-patched", info)?;
    let dest = layout.llama_dir().join(&it.file);
    if matches(&dest, &it, true) {
        return Ok("patched llama.dll present".into());
    }
    let keep = layout.llama_dir().join("llama.dll.release");
    if dest.is_file() && !keep.is_file() {
        fs::copy(&dest, &keep).map_err(|e| e.to_string())?;
    }
    match install(&it, &dest, &Options { from: o.from.clone(), ..Default::default() }) {
        Ok(how) => Ok(format!("patched llama.dll {how}")),
        Err(e) => {
            if keep.is_file() && !dest.is_file() {
                let _ = fs::copy(&keep, &dest);
            }
            Ok(format!("warning: release llama.dll kept, the specialist needs about twice the GPU memory ({e})"))
        }
    }
}

/// Needle 3 into models/needle: the weights, the license and the native engine of this platform, taken out of the
/// pinned wheel (only that one file is read from it; no Python code from the wheel is used).
fn install_needle(layout: &Layout, info: &Map, o: &Options) -> Result<String, String> {
    let dir = layout.models_dir().join("needle");
    let base = format!("https://huggingface.co/{}/resolve/{}/", s(info, "repo").unwrap_or(""), s(info, "revision").unwrap_or(""));
    // a --from folder may hold the files directly or in needle/ (a models folder of 1.x or 2.x)
    let from: Vec<PathBuf> = o.from.iter().flat_map(|d| [d.clone(), d.join("needle")]).collect();
    let opts = Options { from: from.clone(), verify: o.verify, ..Default::default() };
    let file_item = |key: &str, m: &Map| -> Result<Item, String> {
        let file = s(m, "file").ok_or_else(|| format!("needle {key}: no file"))?.to_owned();
        Ok(Item {
            key: format!("needle {key}"),
            url: Some(format!("{base}{file}")),
            bytes: m.get("bytes").and_then(|v| v.to_f64().ok()).unwrap_or(0.0) as u64,
            sha256: s(m, "sha256").unwrap_or("").to_lowercase(),
            file,
        })
    };
    let mut done = Vec::new();
    for key in ["weights", "license_file"] {
        let m = info.get(key).and_then(Value::as_object).ok_or_else(|| format!("needle: no {key} in models.lock.json"))?;
        let it = file_item(key, m)?;
        done.push(install(&it, &dir.join(&it.file), &opts)?);
    }
    let tag = crate::needle::platform().ok_or("Needle has no native engine for this platform")?;
    let entry = info.get("platforms").and_then(Value::as_object).and_then(|p| p.get(tag)).and_then(Value::as_object).ok_or_else(|| format!("Needle has no native engine for {tag}"))?;
    let member = s(entry, "member").ok_or("needle: no member")?;
    let lib = Item {
        key: "needle engine".into(),
        file: crate::needle::LIBRARY.into(),
        bytes: entry.get("bytes").and_then(|v| v.to_f64().ok()).unwrap_or(0.0) as u64,
        sha256: s(entry, "sha256").unwrap_or("").to_lowercase(),
        url: None,
    };
    let dest = dir.join(&lib.file);
    let how = match install(&lib, &dest, &opts) {
        Ok(how) => how,
        Err(_) => {
            let wheel_name = s(entry, "wheel").ok_or("needle: no wheel")?;
            let wheel = Item {
                key: "needle wheel".into(),
                file: Path::new(wheel_name).file_name().and_then(|n| n.to_str()).unwrap_or("needle.whl").to_owned(),
                bytes: 0,
                sha256: s(entry, "wheel_sha256").unwrap_or("").to_lowercase(),
                url: Some(format!("{base}{wheel_name}")),
            };
            let downloads = layout.root.join("runtime").join("downloads");
            let path = downloads.join(&wheel.file);
            install(&wheel, &path, &Options { from: from.clone(), ..Default::default() })?;
            let tmp = downloads.join("needle.unpack");
            let _ = fs::remove_dir_all(&tmp);
            fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
            unpack_member(&path, member, &tmp)?;
            let out = tmp.join(member);
            if !matches(&out, &lib, true) {
                let _ = fs::remove_dir_all(&tmp);
                return Err(format!("{member} in {} does not match models.lock.json", wheel.file));
            }
            fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
            let _ = fs::remove_file(&dest);
            fs::rename(&out, &dest).or_else(|_| fs::copy(&out, &dest).map(|_| ())).map_err(|e| e.to_string())?;
            let _ = fs::remove_dir_all(&tmp);
            format!("taken out of {}", wheel.file)
        }
    };
    Ok(format!("Needle 3 {} in {} (weights {}, license {}, engine {how})", s(info, "engine_version").unwrap_or("?"), dir.display(), done[0], done[1]))
}

/// One member of a zip file (a wheel) with the system tar (bsdtar, Windows 10+ and macOS) or unzip.
fn unpack_member(zip: &Path, member: &str, into: &Path) -> Result<(), String> {
    let tar = if cfg!(windows) { "tar.exe" } else { "bsdtar" };
    if Command::new(tar).arg("-xf").arg(zip).arg("-C").arg(into).arg(member).status().is_ok_and(|s| s.success()) {
        return Ok(());
    }
    if Command::new("unzip").arg("-o").arg("-q").arg(zip).arg(member).arg("-d").arg(into).status().is_ok_and(|s| s.success()) {
        return Ok(());
    }
    Err(format!("could not unpack {member} from {} (needs tar with zip support or unzip)", zip.display()))
}

pub fn run(layout: &Layout, o: &Options) -> Result<(), String> {
    let lock_path = layout.config_dir().join("models.lock.json");
    let text = fs::read_to_string(&lock_path).map_err(|e| format!("{}: {e}", lock_path.display()))?;
    let lock = loads(&text, false).map_err(|e| format!("models.lock.json: {}", e.message))?;
    let runtime = lock.as_object().and_then(|m| m.get("runtime")).and_then(Value::as_object).ok_or("models.lock.json has no runtime")?;
    println!("TypeLogic setup in {}", layout.root.display());
    let mut failed = Vec::new();
    match install_runtime(layout, runtime, o) {
        Ok(m) => println!("runtime     {m}"),
        Err(e) => failed.push(format!("runtime: {e}")),
    }
    if layout.llama_server().is_file() {
        println!("runtime     {}", install_patch(layout, runtime, o)?);
    }
    for it in models_to_install(&lock, o.with_clef)? {
        let dest = layout.models_dir().join(&it.file);
        match install(&it, &dest, o) {
            Ok(how) => println!("{:<11} {} {how}", it.key, it.file),
            Err(e) => failed.push(format!("{}: {e}", it.key)),
        }
    }
    if let Some(info) = lock.as_object().and_then(|m| m.get("needle")).and_then(Value::as_object) {
        match install_needle(layout, info, o) {
            Ok(m) => println!("needle      {m}"),
            Err(e) => failed.push(format!("needle: {e}")),
        }
    }
    if failed.is_empty() {
        println!("Setup complete. Start with: TypeLogic");
        Ok(())
    } else {
        Err(format!("setup incomplete:\n  {}", failed.join("\n  ")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_default_and_optional_models() {
        let lock = loads(include_str!("../../../config/models.lock.json"), false).unwrap();
        let keys = |v: Vec<Item>| v.into_iter().map(|i| i.key).collect::<Vec<_>>();
        assert_eq!(keys(models_to_install(&lock, false).unwrap()), vec!["plumb-q4", "spec-q8"]);
        let all = models_to_install(&lock, true).unwrap();
        assert_eq!(keys(all.clone()), vec!["plumb-q4", "clef-flash-iq4xs-b", "spec-q8"]);
        assert!(all[0].url.as_deref().unwrap().starts_with("https://huggingface.co/crh225/plumb-4b-GGUF/resolve/"));
        assert!(all[2].url.as_deref() == Some("https://github.com/marcusbsilva/TypeLogic-AI/releases/download/v2.0.0/typelogic-specialist-2b-Q8_0.gguf") && all[2].sha256.len() == 64);
    }

    #[test]
    fn installs_from_a_local_folder_after_checking_the_hash() {
        let dir = std::env::temp_dir().join(format!("tl-setup-{}", std::process::id()));
        let src = dir.join("src");
        fs::create_dir_all(&src).unwrap();
        fs::write(src.join("m.gguf"), b"model bytes").unwrap();
        let it = Item { key: "m".into(), file: "m.gguf".into(), bytes: 11, sha256: sha256_file(&src.join("m.gguf")).unwrap(), url: None };
        let dest = dir.join("models").join("m.gguf");
        let o = Options { from: vec![src.clone()], ..Default::default() };
        assert!(install(&it, &dest, &o).unwrap().contains("from"));
        assert_eq!(install(&it, &dest, &o).unwrap(), "present");
        let wrong = Item { sha256: "0".repeat(64), ..it.clone() };
        fs::remove_file(&dest).unwrap();
        assert!(install(&wrong, &dest, &o).unwrap_err().contains("not published"));
        fs::remove_dir_all(dir).unwrap();
    }
}
