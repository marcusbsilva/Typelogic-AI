//! Small helpers without dependencies: random identifiers, UTC timestamps, atomic file writes.
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// 64 random bits: SipHash keyed by the standard library's OS-seeded keys, over a counter, the clock and the process.
fn random_u64() -> u64 {
    let mut h = RandomState::new().build_hasher();
    h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
    h.write_u128(SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
    h.write_u32(std::process::id());
    h.finish()
}

/// `n` random bytes as lowercase hex.
pub fn random_hex(n: usize) -> String {
    let mut out = String::with_capacity(n * 2);
    while out.len() < n * 2 {
        out.push_str(&format!("{:016x}", random_u64()));
    }
    out.truncate(n * 2);
    out
}

/// `uuid.uuid4().hex`: 32 hex characters with the version and variant bits of a random UUID.
pub fn uuid4_hex() -> String {
    let mut s: Vec<u8> = random_hex(16).into_bytes();
    s[12] = b'4';
    s[16] = b"89ab"[(s[16] as usize) % 4];
    String::from_utf8(s).expect("hex")
}

/// Days since 1970-01-01 -> (year, month, day) (proleptic Gregorian).
pub fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// `datetime.now(timezone.utc).isoformat()`, e.g. 2026-10-29T17:25:31.123456+00:00.
pub fn now_iso() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (y, m, day) = civil(secs.div_euclid(86_400));
    let s = secs.rem_euclid(86_400);
    let micros = d.subsec_micros();
    let frac = if micros == 0 { String::new() } else { format!(".{micros:06}") };
    format!("{y:04}-{m:02}-{day:02}T{:02}:{:02}:{:02}{frac}+00:00", s / 3600, s % 3600 / 60, s % 60)
}

/// Today's local date (year, month, day): the archive names follow the user's calendar.
pub fn today_local() -> (i64, u32, u32) {
    #[cfg(windows)]
    {
        #[repr(C)]
        #[derive(Default)]
        struct SystemTime16 {
            year: u16,
            month: u16,
            day_of_week: u16,
            day: u16,
            hour: u16,
            minute: u16,
            second: u16,
            millis: u16,
        }
        extern "system" {
            fn GetLocalTime(t: *mut SystemTime16);
        }
        let mut t = SystemTime16::default();
        unsafe { GetLocalTime(&mut t) };
        (t.year as i64, t.month as u32, t.day as u32)
    }
    #[cfg(unix)]
    {
        #[repr(C)]
        struct Tm {
            sec: i32,
            min: i32,
            hour: i32,
            mday: i32,
            mon: i32,
            year: i32,
            wday: i32,
            yday: i32,
            isdst: i32,
            gmtoff: i64,
            zone: *const u8,
        }
        extern "C" {
            fn time(t: *mut i64) -> i64;
            fn localtime_r(t: *const i64, out: *mut Tm) -> *mut Tm;
        }
        let mut tm = Tm { sec: 0, min: 0, hour: 0, mday: 0, mon: 0, year: 0, wday: 0, yday: 0, isdst: 0, gmtoff: 0, zone: std::ptr::null() };
        let now = unsafe { time(std::ptr::null_mut()) };
        if unsafe { localtime_r(&now, &mut tm) }.is_null() {
            let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
            return civil(secs.div_euclid(86_400));
        }
        (tm.year as i64 + 1900, tm.mon as u32 + 1, tm.mday as u32)
    }
}

/// Write through a temporary file and rename, so a reader never sees half a file.
pub fn write_atomic(path: &Path, data: &[u8]) -> io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension(format!("tmp-{}", random_hex(4)));
    std::fs::write(&tmp, data)?;
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_and_dates() {
        let a = uuid4_hex();
        assert_eq!(a.len(), 32);
        assert_eq!(&a[12..13], "4");
        assert_ne!(a, uuid4_hex());
        assert_eq!(civil(0), (1970, 1, 1));
        assert_eq!(civil(19_782), (2024, 2, 29));
        assert_eq!(civil(-1), (1969, 12, 31));
        let now = now_iso();
        assert!(now.ends_with("+00:00") && now.len() >= 25, "{now}");
    }
}
