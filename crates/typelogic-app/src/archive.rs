//! Library limit and archive (port of `typelogic/archive.py`): at most LIMIT decisions are kept; then every decision
//! is written into one archive in `data/archives`, named after the day (DD-MM-YYYY.rar, DD-MM-YYYY-2.rar, ...).
//!
//! The archive holds one JSON file per decision (the stored record with its integrity check, corrections and audit
//! trail) under decisions/<workspace>/, plus manifest.json with the SHA-256 of every file. RAR needs WinRAR's
//! command-line tool (Rar.exe: PATH, the usual install folders or TYPELOGIC_RAR); without it the archive is a .zip
//! written here. Decisions leave the library only after the archive has been written and tested.
use crate::library::Library;
use crate::util::{now_iso, today_local, write_atomic};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use typelogic_core::hash::{hex, sha256};
use typelogic_core::json::{dumps_indent, DumpOptions, Map, Value};

pub const LIMIT: usize = 200;

/// The library limit: 200, or a smaller TYPELOGIC_LIBRARY_LIMIT (tests of the archive dialog).
pub fn limit() -> usize { std::env::var("TYPELOGIC_LIBRARY_LIMIT").ok().and_then(|v| v.parse().ok()).filter(|n| (1..=LIMIT).contains(n)).unwrap_or(LIMIT) }

pub fn folder(data: &Path) -> PathBuf { data.join("archives") }

/// WinRAR's command-line tool, or None.
pub fn rar_executable() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("TYPELOGIC_RAR") {
        let p = PathBuf::from(p);
        return p.is_file().then_some(p);
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    for dir in std::env::split_paths(&path) {
        for name in ["rar", "Rar.exe", "rar.exe"] {
            let p = dir.join(name);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    if cfg!(windows) {
        for var in ["ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"] {
            if let Some(base) = std::env::var_os(var) {
                let p = PathBuf::from(base).join("WinRAR").join("Rar.exe");
                if p.is_file() {
                    return Some(p);
                }
            }
        }
    }
    None
}

/// `<folder>/DD-MM-YYYY.<ext>`, with -2, -3, ... when the name is taken.
pub fn next_path(folder: &Path, ext: &str, today: (i64, u32, u32)) -> PathBuf {
    let base = format!("{:02}-{:02}-{:04}", today.2, today.1, today.0);
    let mut path = folder.join(format!("{base}.{ext}"));
    let mut n = 2;
    while path.exists() {
        path = folder.join(format!("{base}-{n}.{ext}"));
        n += 1;
    }
    path
}

/// GET /api/archive: what the limit and the next archive look like.
pub fn info(lib: &Library, data: &Path) -> Value {
    let count = lib.count();
    let rar = rar_executable().is_some();
    let kind = if rar { "rar" } else { "zip" };
    let path = next_path(&folder(data), kind, today_local());
    let mut m = Map::new();
    m.push_unique("count", Value::int(count as i64));
    m.push_unique("limit", Value::int(limit() as i64));
    m.push_unique("full", Value::Bool(count >= limit()));
    m.push_unique("format", Value::str(kind));
    m.push_unique("folder", Value::Str(folder(data).display().to_string()));
    m.push_unique("name", Value::Str(path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()));
    m.push_unique("note", if rar { Value::Null } else { Value::str("WinRAR (Rar.exe) was not found, so the archive will be a .zip file") });
    Value::Object(m)
}

/// The message of a full library, or None when there is room.
pub fn full_message(lib: &Library, data: &Path) -> Option<String> {
    if lib.count() < limit() {
        return None;
    }
    let i = info(lib, data);
    let m = i.as_object()?;
    let path = PathBuf::from(m.get("folder")?.as_str()?).join(m.get("name")?.as_str()?);
    Some(format!(
        "The library holds {} decisions, the maximum. Archive them to continue: every decision is compressed into {} and removed from the library.",
        limit(),
        path.display()
    ))
}

fn safe(name: &str) -> String {
    let mut out = String::new();
    let mut in_run = false;
    for c in name.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('_');
            in_run = true;
        }
    }
    let trimmed = out.trim_matches(|c| c == '.' || c == '_');
    if trimmed.is_empty() { "workspace".into() } else { trimmed.into() }
}

// ---------------------------------------------------------------- zip (stored entries)

fn crc32(data: &[u8]) -> u32 {
    let mut table = [0u32; 256];
    for (i, t) in table.iter_mut().enumerate() {
        let mut c = i as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
        *t = c;
    }
    let mut crc = 0xFFFF_FFFFu32;
    for b in data {
        crc = table[((crc ^ *b as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    !crc
}

/// A zip archive of stored (uncompressed) entries with UTF-8 names.
pub fn write_zip(target: &Path, files: &[(String, Vec<u8>)]) -> io::Result<()> {
    let mut out = Vec::new();
    let mut central = Vec::new();
    let le16 = |v: &mut Vec<u8>, x: u16| v.extend_from_slice(&x.to_le_bytes());
    let le32 = |v: &mut Vec<u8>, x: u32| v.extend_from_slice(&x.to_le_bytes());
    for (name, data) in files {
        if data.len() > u32::MAX as usize || out.len() > u32::MAX as usize {
            return Err(io::Error::other("archive too large for a zip file"));
        }
        let (crc, offset) = (crc32(data), out.len() as u32);
        le32(&mut out, 0x0403_4b50);
        le16(&mut out, 20);
        le16(&mut out, 0x0800); // UTF-8 names
        le16(&mut out, 0); // stored
        le16(&mut out, 0);
        le16(&mut out, 0x21); // 1980-01-01
        le32(&mut out, crc);
        le32(&mut out, data.len() as u32);
        le32(&mut out, data.len() as u32);
        le16(&mut out, name.len() as u16);
        le16(&mut out, 0);
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(data);
        le32(&mut central, 0x0201_4b50);
        le16(&mut central, 20);
        le16(&mut central, 20);
        le16(&mut central, 0x0800);
        le16(&mut central, 0);
        le16(&mut central, 0);
        le16(&mut central, 0x21);
        le32(&mut central, crc);
        le32(&mut central, data.len() as u32);
        le32(&mut central, data.len() as u32);
        le16(&mut central, name.len() as u16);
        le16(&mut central, 0);
        le16(&mut central, 0);
        le16(&mut central, 0);
        le16(&mut central, 0);
        le32(&mut central, 0);
        le32(&mut central, offset);
        central.extend_from_slice(name.as_bytes());
    }
    let (cd_offset, cd_size) = (out.len() as u32, central.len() as u32);
    out.extend_from_slice(&central);
    le32(&mut out, 0x0605_4b50);
    le16(&mut out, 0);
    le16(&mut out, 0);
    le16(&mut out, files.len() as u16);
    le16(&mut out, files.len() as u16);
    le32(&mut out, cd_size);
    le32(&mut out, cd_offset);
    le16(&mut out, 0);
    let mut f = fs::OpenOptions::new().write(true).create_new(true).open(target)?;
    f.write_all(&out)?;
    f.sync_all()
}

/// Read a stored zip back and check every entry's CRC (`ZipFile.testzip`); returns the entries.
pub fn test_zip(target: &Path) -> io::Result<Vec<(String, Vec<u8>)>> {
    let data = fs::read(target)?;
    let bad = || io::Error::other("The archive failed its integrity test");
    let u16_at = |i: usize| data.get(i..i + 2).map(|b| u16::from_le_bytes([b[0], b[1]]) as usize).ok_or_else(bad);
    let u32_at = |i: usize| data.get(i..i + 4).map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]])).ok_or_else(bad);
    let mut entries = Vec::new();
    let mut i = 0;
    while u32_at(i)? == 0x0403_4b50 {
        let crc = u32_at(i + 14)?;
        let size = u32_at(i + 18)? as usize;
        let (n, extra) = (u16_at(i + 26)?, u16_at(i + 28)?);
        let name = String::from_utf8(data.get(i + 30..i + 30 + n).ok_or_else(bad)?.to_vec()).map_err(|_| bad())?;
        let start = i + 30 + n + extra;
        let body = data.get(start..start + size).ok_or_else(bad)?.to_vec();
        if crc32(&body) != crc {
            return Err(bad());
        }
        entries.push((name, body));
        i = start + size;
    }
    if u32_at(i)? != 0x0201_4b50 && !entries.is_empty() {
        return Err(bad());
    }
    Ok(entries)
}

/// Compress every decision into `data/archives` and remove them from the library (POST /api/archive).
pub fn archive(lib: &mut Library, data: &Path, version: &str) -> Result<Value, String> {
    let rows = lib.archive_rows();
    if rows.is_empty() {
        return Err("The library is empty; there is nothing to archive".into());
    }
    let target_folder = folder(data);
    fs::create_dir_all(&target_folder).map_err(|e| e.to_string())?;
    let rar = rar_executable();
    let kind = if rar.is_some() { "rar" } else { "zip" };
    let target = next_path(&target_folder, kind, today_local());
    let mut files: Vec<(String, Vec<u8>)> = Vec::new();
    let mut hashes = Map::new();
    let mut spaces: Vec<String> = Vec::new();
    for (id, space, record) in &rows {
        let name = format!("decisions/{}/{id}.json", safe(space));
        let bytes = dumps_indent(record, 2, DumpOptions::REPLY).map_err(|e| e.message)?.into_bytes();
        hashes.push_unique(name.clone(), Value::Str(hex(&sha256(&bytes))));
        files.push((name, bytes));
        if !spaces.contains(space) {
            spaces.push(space.clone());
        }
    }
    spaces.sort();
    let mut manifest = Map::new();
    manifest.push_unique("format", Value::str("typelogic-archive-v1"));
    manifest.push_unique("app_version", Value::str(version));
    manifest.push_unique("created", Value::Str(now_iso()));
    manifest.push_unique("decisions", Value::int(rows.len() as i64));
    manifest.push_unique("workspaces", Value::Array(spaces.into_iter().map(Value::Str).collect()));
    manifest.push_unique("files_sha256", Value::Object(hashes));
    files.push(("manifest.json".into(), dumps_indent(&Value::Object(manifest), 2, DumpOptions::REPLY).map_err(|e| e.message)?.into_bytes()));
    let written = match &rar {
        Some(exe) => (|| -> io::Result<()> {
            let tmp = target_folder.join(format!(".typelogic-archive-{}", crate::util::random_hex(4)));
            for (name, bytes) in &files {
                write_atomic(&tmp.join(name), bytes)?;
            }
            let mut a = Command::new(exe);
            a.args(["a", "-r", "-ep1", "-m5", "-idq", "-y"]).arg(&target).arg(format!("{}{}*", tmp.display(), std::path::MAIN_SEPARATOR));
            let mut t = Command::new(exe);
            t.args(["t", "-idq"]).arg(&target);
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                a.creation_flags(0x0800_0000);
                t.creation_flags(0x0800_0000);
            }
            let ok = a.status()?.success() && t.status()?.success();
            let _ = fs::remove_dir_all(&tmp);
            if ok { Ok(()) } else { Err(io::Error::other("Rar.exe failed")) }
        })(),
        None => write_zip(&target, &files).and_then(|_| test_zip(&target).map(|_| ())),
    };
    if let Err(e) = written {
        let _ = fs::remove_file(&target);
        return Err(format!("Archive failed; nothing was removed: {e}"));
    }
    let ids: Vec<String> = rows.iter().map(|(id, _, _)| id.clone()).collect();
    lib.remove_all(&ids)?;
    let mut m = Map::new();
    m.push_unique("path", Value::Str(target.display().to_string()));
    m.push_unique("name", Value::Str(target.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()));
    m.push_unique("format", Value::str(kind));
    m.push_unique("decisions", Value::int(ids.len() as i64));
    m.push_unique("external_action_executed", Value::Bool(false));
    Ok(Value::Object(m))
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::json::loads;

    #[test]
    fn zip_roundtrip_and_names() {
        let dir = std::env::temp_dir().join(format!("tl-arch-{}", crate::util::random_hex(4)));
        fs::create_dir_all(&dir).unwrap();
        let files = vec![("decisions/default/a.json".to_string(), b"{\"x\": 1}".to_vec()), ("manifest.json".to_string(), "é".as_bytes().to_vec())];
        let p = next_path(&dir, "zip", (2026, 10, 29));
        assert!(p.ends_with("29-10-2026.zip"));
        write_zip(&p, &files).unwrap();
        assert_eq!(test_zip(&p).unwrap(), files);
        assert!(next_path(&dir, "zip", (2026, 10, 29)).ends_with("29-10-2026-2.zip"));
        assert!(write_zip(&p, &files).is_err()); // never overwritten
        let mut bytes = fs::read(&p).unwrap();
        bytes[60] ^= 1;
        let q = dir.join("bad.zip");
        fs::write(&q, bytes).unwrap();
        assert!(test_zip(&q).is_err());
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(safe("team / ops.."), "team_ops");
        assert_eq!(safe("..."), "workspace");
        let mut lib = Library::open(&dir.join("library")).unwrap();
        assert!(archive(&mut lib, &dir, "t").unwrap_err().contains("empty"));
        let req = loads(r#"{"state":"x","questions":{"q":{"type":"noul","instructions":"ok?"}}}"#, false).unwrap();
        let resp = loads(r#"{"answers":{}}"#, false).unwrap();
        lib.add(req.clone(), resp.clone(), "A", "default").unwrap();
        lib.add(req, resp, "B", "team x").unwrap();
        if rar_executable().is_none() {
            let r = archive(&mut lib, &dir, "t").unwrap();
            let path = PathBuf::from(r.as_object().unwrap().get("path").unwrap().as_str().unwrap());
            let entries = test_zip(&path).unwrap();
            assert_eq!(entries.len(), 3);
            assert!(entries.iter().any(|(n, _)| n.starts_with("decisions/team_x/")));
            assert_eq!(lib.count(), 0);
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
