//! Lua 5.4 C module for `typelogic-core` (`require "typelogic_core"`), using the stable Lua C API directly.
//!
//! Functions never raise: `core.call(method, params_json)` returns `response_json` (which may itself carry
//! `{"error": ...}`) or `nil, message` for bad arguments, so no `longjmp` ever crosses Rust frames.
//! `sdk/lua/typelogic.lua` turns errors into Lua `error()` calls. Lua API symbols are resolved at load time
//! from the host interpreter (`dlsym` / `GetProcAddress`), so no Lua import library is needed to build.
#![allow(non_camel_case_types)]
// Function pointers are looked up by name and transmuted to the field's exact C signature declared above.
#![allow(clippy::missing_transmute_annotations)]
use std::ffi::{c_char, c_int, c_void, CString};
use std::ptr;
use std::sync::OnceLock;

type lua_State = c_void;
type lua_CFunction = unsafe extern "C" fn(*mut lua_State) -> c_int;
const LUA_TSTRING: c_int = 4;

struct Lua {
    type_: unsafe extern "C" fn(*mut lua_State, c_int) -> c_int,
    tolstring: unsafe extern "C" fn(*mut lua_State, c_int, *mut usize) -> *const c_char,
    pushlstring: unsafe extern "C" fn(*mut lua_State, *const c_char, usize) -> *const c_char,
    pushnil: unsafe extern "C" fn(*mut lua_State),
    createtable: unsafe extern "C" fn(*mut lua_State, c_int, c_int),
    pushcclosure: unsafe extern "C" fn(*mut lua_State, lua_CFunction, c_int),
    setfield: unsafe extern "C" fn(*mut lua_State, c_int, *const c_char),
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
    // Common Windows Lua 5.4 builds: lua54.dll (official binaries), lua5.4.dll, lua.dll, or a static interpreter.
    for module in [b"lua54.dll\0".as_ptr(), b"lua5.4.dll\0".as_ptr(), b"lua.dll\0".as_ptr(), ptr::null()] {
        let handle = GetModuleHandleA(module as *const c_char);
        if !handle.is_null() {
            let f = GetProcAddress(handle, symbol.as_ptr());
            if !f.is_null() {
                return f;
            }
        }
    }
    ptr::null_mut()
}

fn lua() -> Option<&'static Lua> {
    static API: OnceLock<Option<Lua>> = OnceLock::new();
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
        Some(Lua {
            type_: get!("lua_type"),
            tolstring: get!("lua_tolstring"),
            pushlstring: get!("lua_pushlstring"),
            pushnil: get!("lua_pushnil"),
            createtable: get!("lua_createtable"),
            pushcclosure: get!("lua_pushcclosure"),
            setfield: get!("lua_setfield"),
        })
    })
    .as_ref()
}

unsafe fn arg(api: &Lua, l: *mut lua_State, index: c_int) -> Option<String> {
    if (api.type_)(l, index) != LUA_TSTRING {
        return None;
    }
    let mut len = 0usize;
    let p = (api.tolstring)(l, index, &mut len);
    if p.is_null() {
        return None;
    }
    let bytes = std::slice::from_raw_parts(p as *const u8, len);
    String::from_utf8(bytes.to_vec()).ok()
}

unsafe fn push(api: &Lua, l: *mut lua_State, text: &str) { (api.pushlstring)(l, text.as_ptr() as *const c_char, text.len()); }

unsafe extern "C" fn lua_call(l: *mut lua_State) -> c_int {
    let Some(api) = lua() else { return 0 };
    let (Some(method), Some(params)) = (arg(api, l, 1), arg(api, l, 2)) else {
        (api.pushnil)(l);
        push(api, l, "call(method, params_json) expects two UTF-8 strings");
        return 2;
    };
    let response = typelogic_core::api::call(&method, &params);
    push(api, l, &response);
    1
}

unsafe extern "C" fn lua_version(l: *mut lua_State) -> c_int {
    let Some(api) = lua() else { return 0 };
    push(api, l, typelogic_core::VERSION);
    1
}

/// `require "typelogic_core"` entry point.
///
/// # Safety
/// Called by the Lua interpreter with a valid `lua_State` (Lua 5.4 C API).
#[no_mangle]
pub unsafe extern "C" fn luaopen_typelogic_core(l: *mut lua_State) -> c_int {
    let Some(api) = lua() else { return 0 };
    (api.createtable)(l, 0, 2);
    for (name, f) in [("call", lua_call as lua_CFunction), ("version", lua_version as lua_CFunction)] {
        (api.pushcclosure)(l, f, 0);
        let cname = CString::new(name).unwrap();
        (api.setfield)(l, -2, cname.as_ptr());
    }
    1
}
