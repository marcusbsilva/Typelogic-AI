//! Embeds the built-in decision templates (templates/builtin/*.json) into TypeLogic.exe.
use std::fmt::Write as _;
use std::path::Path;

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../templates/builtin");
    println!("cargo:rerun-if-changed={}", root.display());
    let mut names: Vec<_> = std::fs::read_dir(&root)
        .map(|d| d.filter_map(|e| e.ok()).map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "json")).collect())
        .unwrap_or_default();
    names.sort();
    let mut out = String::from("/// (file name, contents) of the built-in templates.\npub const BUILTIN: &[(&str, &str)] = &[\n");
    for path in names {
        println!("cargo:rerun-if-changed={}", path.display());
        let name = path.file_name().and_then(|n| n.to_str()).expect("utf-8 file name");
        let abs = path.canonicalize().expect("template path");
        let _ = writeln!(out, "    ({name:?}, include_str!({:?})),", abs.display().to_string());
    }
    out.push_str("];\n");
    let dest = Path::new(&std::env::var("OUT_DIR").expect("OUT_DIR")).join("builtin_templates.rs");
    std::fs::write(dest, out).expect("write builtin_templates.rs");
}
