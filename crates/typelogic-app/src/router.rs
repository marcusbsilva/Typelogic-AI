//! Supervisor of llama-server in router mode: one child process on 127.0.0.1 that loads and unloads the models of
//! models.ini on demand. The child is tied to TypeLogic.exe (a Windows job object kills it when TypeLogic exits).
use crate::http;
use crate::layout::{parse_devices, Gpu, Layout, Plan};
use std::fs::File;
use std::io;
use std::net::{SocketAddr, TcpListener};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub struct Router {
    child: Child,
    pub addr: SocketAddr,
}

/// The GPUs llama.cpp can use (`llama-server --list-devices`); empty when there is none or the runtime is missing.
pub fn gpus(layout: &Layout) -> (Vec<Gpu>, String) {
    let mut command = Command::new(layout.llama_server());
    command.arg("--list-devices").current_dir(layout.llama_dir()).stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    match command.output() {
        Ok(out) => {
            let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
            (parse_devices(&text), text)
        }
        Err(e) => (Vec::new(), e.to_string()),
    }
}

/// Start a child process. On Linux the inference server must die with TypeLogic (PR_SET_PDEATHSIG), but that
/// signal follows the *thread* that spawned the child: the models load on short-lived threads, so the spawn is done
/// by one thread that lives as long as the process.
#[cfg(target_os = "linux")]
fn spawn(command: Command) -> io::Result<Child> {
    use std::sync::mpsc::{channel, Sender};
    use std::sync::{Mutex, OnceLock};
    type Job = (Command, Sender<io::Result<Child>>);
    static SPAWNER: OnceLock<Mutex<Sender<Job>>> = OnceLock::new();
    let spawner = SPAWNER.get_or_init(|| {
        let (tx, rx) = channel::<Job>();
        std::thread::spawn(move || {
            while let Ok((mut command, reply)) = rx.recv() {
                let _ = reply.send(command.spawn());
            }
        });
        Mutex::new(tx)
    });
    let (tx, rx) = channel();
    spawner.lock().unwrap_or_else(|e| e.into_inner()).send((command, tx)).map_err(|_| io::Error::other("spawner thread stopped"))?;
    rx.recv().map_err(|_| io::Error::other("spawner thread stopped"))?
}

#[cfg(not(target_os = "linux"))]
fn spawn(mut command: Command) -> io::Result<Child> { command.spawn() }

/// A free loopback port (bound then released).
pub fn free_port() -> io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

impl Router {
    pub fn start(layout: &Layout, plan: &Plan) -> io::Result<Router> {
        let server = layout.llama_server();
        if !server.is_file() {
            return Err(io::Error::new(io::ErrorKind::NotFound, format!("{} is missing; run: TypeLogic setup", server.display())));
        }
        std::fs::create_dir_all(layout.config_dir())?;
        std::fs::create_dir_all(layout.logs_dir())?;
        std::fs::write(layout.presets(), layout.presets_text(plan))?;
        let port = free_port()?;
        let log = File::create(layout.logs_dir().join("llama-server.log"))?;
        let mut command = Command::new(&server);
        command
            .current_dir(layout.llama_dir())
            .args(["--host", "127.0.0.1", "--port", &port.to_string(), "--models-preset"])
            .arg(layout.presets())
            // --offline: never fetch anything at run time; models come only from models.ini.
            // --models-memory-margin 0: no admission control by the router. Its memory estimate counts the LM head
            // of decision models (5.1 GiB for the specialist, 2.0 GiB measured with the patched llama.dll), so it
            // refuses combinations that fit; TypeLogic chooses the models from measured figures instead (Plan).
            .args(["--models-max", &plan.models.len().max(1).to_string(), "--models-memory-margin", "0", "--no-webui", "--offline"])
            .stdin(Stdio::null())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::process::CommandExt;
            extern "C" {
                fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
            }
            // PR_SET_PDEATHSIG = 1, SIGKILL = 9: the server dies with TypeLogic, even when TypeLogic is killed
            unsafe {
                command.pre_exec(|| {
                    prctl(1, 9, 0, 0, 0);
                    Ok(())
                });
            }
        }
        let child = spawn(command)?;
        #[cfg(windows)]
        crate::job::kill_with_parent(&child);
        Ok(Router { child, addr: SocketAddr::from(([127, 0, 0, 1], port)) })
    }

    /// Wait until the router answers and every model of the plan is loaded (status of GET /models).
    /// A model that fails to load is reported; the others stay usable.
    pub fn wait_ready(&mut self, models: &[&str], timeout: Duration) -> io::Result<Vec<String>> {
        let began = Instant::now();
        while began.elapsed() < timeout {
            if let Some(status) = self.child.try_wait()? {
                return Err(io::Error::other(format!("llama-server exited ({status}); see logs/llama-server.log")));
            }
            if let Ok((200, body)) = http::call(self.addr, "GET", "/models", None, Duration::from_secs(5)) {
                let states = model_states(&String::from_utf8_lossy(&body));
                let state = |m: &str| states.iter().find(|(id, _)| id == m).map(|(_, s)| s.as_str()).unwrap_or("");
                if models.iter().all(|m| matches!(state(m), "loaded" | "failed")) {
                    return Ok(models.iter().filter(|m| state(m) != "loaded").map(|m| m.to_string()).collect());
                }
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        Err(io::Error::new(io::ErrorKind::TimedOut, "the models did not load in time; see logs/llama-server.log"))
    }

}

impl Drop for Router {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// (model id, status) pairs of the router's GET /models reply.
fn model_states(text: &str) -> Vec<(String, String)> {
    use typelogic_core::json::{loads, Value};
    let Ok(v) = loads(text, false) else { return Vec::new() };
    let Some(data) = v.as_object().and_then(|m| m.get("data")).and_then(Value::as_array) else { return Vec::new() };
    data.iter()
        .filter_map(|m| {
            let m = m.as_object()?;
            let id = m.get("id")?.as_str()?.to_owned();
            let status = m.get("status").and_then(Value::as_object);
            let failed = status.and_then(|s| s.get("failed")).is_some_and(|f| matches!(f, Value::Bool(true)));
            let value = status.and_then(|s| s.get("value")).and_then(Value::as_str).unwrap_or("").to_owned();
            Some((id, if failed { "failed".to_owned() } else { value }))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_model_states() {
        let s = model_states(r#"{"data":[{"id":"generalist","status":{"value":"loaded"}},{"id":"clef","status":{"value":"unloaded","failed":true}}]}"#);
        assert_eq!(s, vec![("generalist".into(), "loaded".into()), ("clef".into(), "failed".into())]);
    }
}
