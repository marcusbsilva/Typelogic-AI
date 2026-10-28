//! Node.js N-API addon for `typelogic-core`, written against the stable C ABI without extra crates.
//!
//! Exports `version()` and `call(method, paramsJson) -> responseJson`; `sdk/typescript/src/native.ts`
//! wraps them with types. N-API functions are resolved at load time from the running Node.js process
//! (`dlsym` / `GetProcAddress`), so the same build works without `node.lib` import libraries.
#![allow(non_camel_case_types)]
// Function pointers are looked up by name and transmuted to the field's exact C signature declared above.
#![allow(clippy::missing_transmute_annotations)]
use std::ffi::{c_char, c_void, CString};
use std::ptr;
use std::sync::OnceLock;

type napi_env = *mut c_void;
type napi_value = *mut c_void;
type napi_callback_info = *mut c_void;
type napi_status = i32;
type napi_callback = unsafe extern "C" fn(napi_env, napi_callback_info) -> napi_value;

struct Napi {
    get_cb_info: unsafe extern "C" fn(napi_env, napi_callback_info, *mut usize, *mut napi_value, *mut napi_value, *mut *mut c_void) -> napi_status,
    get_value_string_utf8: unsafe extern "C" fn(napi_env, napi_value, *mut c_char, usize, *mut usize) -> napi_status,
    create_string_utf8: unsafe extern "C" fn(napi_env, *const c_char, usize, *mut napi_value) -> napi_status,
    create_function: unsafe extern "C" fn(napi_env, *const c_char, usize, napi_callback, *mut c_void, *mut napi_value) -> napi_status,
    set_named_property: unsafe extern "C" fn(napi_env, napi_value, *const c_char, napi_value) -> napi_status,
    throw_error: unsafe extern "C" fn(napi_env, *const c_char, *const c_char) -> napi_status,
}

#[cfg(unix)]
unsafe fn lookup(name: &str) -> *mut c_void {
    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    }
    #[cfg(target_os = "macos")]
    const RTLD_DEFAULT: *mut c_void = -2isize as *mut c_void;
    #[cfg(not(target_os = "macos"))]
    const RTLD_DEFAULT: *mut c_void = ptr::null_mut();
    let symbol = CString::new(name).unwrap();
    dlsym(RTLD_DEFAULT, symbol.as_ptr())
}

#[cfg(windows)]
unsafe fn lookup(name: &str) -> *mut c_void {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleA(name: *const c_char) -> *mut c_void;
        fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
    }
    let symbol = CString::new(name).unwrap();
    // N-API lives in the host executable (node.exe); Electron-style hosts export it from node.dll.
    for module in [ptr::null(), b"node.dll\0".as_ptr() as *const c_char] {
        let handle = GetModuleHandleA(module);
        if !handle.is_null() {
            let f = GetProcAddress(handle, symbol.as_ptr());
            if !f.is_null() {
                return f;
            }
        }
    }
    ptr::null_mut()
}

fn napi() -> Option<&'static Napi> {
    static API: OnceLock<Option<Napi>> = OnceLock::new();
    API.get_or_init(|| unsafe {
        macro_rules! get {
            ($name:literal) => {{
                let f = lookup($name);
                if f.is_null() {
                    return None;
                }
                std::mem::transmute::<*mut c_void, _>(f)
            }};
        }
        Some(Napi {
            get_cb_info: get!("napi_get_cb_info"),
            get_value_string_utf8: get!("napi_get_value_string_utf8"),
            create_string_utf8: get!("napi_create_string_utf8"),
            create_function: get!("napi_create_function"),
            set_named_property: get!("napi_set_named_property"),
            throw_error: get!("napi_throw_error"),
        })
    })
    .as_ref()
}

unsafe fn throw(api: &Napi, env: napi_env, message: &str) -> napi_value {
    let code = CString::new("TYPELOGIC_CORE").unwrap();
    let msg = CString::new(message.replace('\0', " ")).unwrap();
    (api.throw_error)(env, code.as_ptr(), msg.as_ptr());
    ptr::null_mut()
}

unsafe fn arg_string(api: &Napi, env: napi_env, value: napi_value) -> Option<String> {
    let mut len = 0usize;
    if (api.get_value_string_utf8)(env, value, ptr::null_mut(), 0, &mut len) != 0 {
        return None; // not a string
    }
    let mut buf = vec![0u8; len + 1];
    let mut written = 0usize;
    if (api.get_value_string_utf8)(env, value, buf.as_mut_ptr() as *mut c_char, len + 1, &mut written) != 0 {
        return None;
    }
    buf.truncate(written);
    String::from_utf8(buf).ok()
}

unsafe fn make_string(api: &Napi, env: napi_env, text: &str) -> napi_value {
    let mut out: napi_value = ptr::null_mut();
    if (api.create_string_utf8)(env, text.as_ptr() as *const c_char, text.len(), &mut out) != 0 {
        return throw(api, env, "could not create result string");
    }
    out
}

unsafe extern "C" fn js_call(env: napi_env, info: napi_callback_info) -> napi_value {
    let Some(api) = napi() else { return ptr::null_mut() };
    let mut argc = 2usize;
    let mut argv: [napi_value; 2] = [ptr::null_mut(); 2];
    if (api.get_cb_info)(env, info, &mut argc, argv.as_mut_ptr(), ptr::null_mut(), ptr::null_mut()) != 0 || argc < 2 {
        return throw(api, env, "call(method, paramsJson) expects two strings");
    }
    let (Some(method), Some(params)) = (arg_string(api, env, argv[0]), arg_string(api, env, argv[1])) else {
        return throw(api, env, "call(method, paramsJson) expects two strings");
    };
    let response = typelogic_core::api::call(&method, &params);
    make_string(api, env, &response)
}

unsafe extern "C" fn js_version(env: napi_env, _info: napi_callback_info) -> napi_value {
    let Some(api) = napi() else { return ptr::null_mut() };
    make_string(api, env, typelogic_core::VERSION)
}

unsafe fn export(api: &Napi, env: napi_env, exports: napi_value, name: &str, f: napi_callback) {
    let cname = CString::new(name).unwrap();
    let mut func: napi_value = ptr::null_mut();
    if (api.create_function)(env, cname.as_ptr(), name.len(), f, ptr::null_mut(), &mut func) == 0 {
        (api.set_named_property)(env, exports, cname.as_ptr(), func);
    }
}

/// Entry point Node.js looks up when loading a `.node` file.
///
/// # Safety
/// Called by Node.js with a valid `env` and `exports` object for this module instance.
#[no_mangle]
pub unsafe extern "C" fn napi_register_module_v1(env: napi_env, exports: napi_value) -> napi_value {
    let Some(api) = napi() else { return exports };
    export(api, env, exports, "call", js_call);
    export(api, env, exports, "version", js_version);
    exports
}

/// Highest N-API version this addon relies on (Node.js reads it when present).
#[no_mangle]
pub extern "C" fn node_api_module_get_api_version_v1() -> i32 { 8 }
