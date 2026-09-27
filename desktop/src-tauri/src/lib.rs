//! Splitter native desktop app (Tauri 2) — the **portable, standalone** release form.
//!
//! The Python app is untouched; this shell is packaging, in GridFPV's shape:
//!
//! 1. Resolve a **true-portable data dir**: `splitter-data/` beside the running executable
//!    when that is writable, else the per-user app-data dir. The SQLite database, the
//!    extracted sidecar and the log file all live there, so a copied exe carries its data.
//! 2. Unpack the **embedded sidecar** (the one-dir PyInstaller build of the Python server,
//!    packed as a tar.gz and baked into this binary at compile time by `build.rs`) into
//!    `<data>/bin/splitter-sidecar-<version>/`, once per version. One-dir rather than
//!    one-file so nothing self-extracts into a temp folder on every launch — the
//!    behaviour that makes unsigned PyInstaller one-file exes an antivirus false
//!    positive (docs/code-signing.md).
//! 3. **Attach if a Splitter is already running** (sync phase 3, #13): a `node.port`
//!    marker in the service's data dir (`C:\ProgramData\Splitter` and friends —
//!    `splitter/service.py::default_data_dir`) or in our own, whose `/healthz` answers,
//!    means the server is up (installed as a service, or another window's sidecar). Then
//!    this is just a window onto it: no unpack, no spawn, nothing to kill.
//! 4. Otherwise spawn the sidecar with `SPLITTER_DATA_DIR` set, read its stdout until it
//!    prints `SPLITTER_READY <url>`, and open the main window at that URL. The sidecar binds
//!    all interfaces on 8100 (fallback: ephemeral) so a tablet on the LAN can open the same
//!    pages; the window itself always uses loopback.
//! 5. Kill the sidecar when the app exits (only the one we started).
//!
//! Diagnostics: release builds on Windows have no console (`main.rs`), so everything
//! this shell wants to say goes to `<data>/splitter-desktop.log` as well as stderr.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

/// The sidecar archive (tar.gz of the one-dir build), embedded at build time (see `build.rs`).
static SIDECAR: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/sidecar.bin"));
/// Top-level directory inside the archive and the executable's name (desktop/sidecar/build.py).
const SIDECAR_NAME: &str = "splitter-sidecar";
/// Written into the unpacked folder last; holds the archive size so a half-finished or
/// differently built unpack is redone.
const SIDECAR_MARKER: &str = ".unpacked";
const SIDECAR_EMBEDDED: bool = matches!(env!("SPLITTER_SIDECAR_EMBEDDED").as_bytes(), b"1");
const READY_TIMEOUT: Duration = Duration::from_secs(60);

static LOG: OnceLock<Mutex<Option<File>>> = OnceLock::new();

/// Log a line to stderr and to the data-dir log file (once it is open).
fn log(msg: impl AsRef<str>) {
    let msg = msg.as_ref();
    eprintln!("splitter-desktop: {msg}");
    if let Some(m) = LOG.get() {
        if let Ok(mut guard) = m.lock() {
            if let Some(f) = guard.as_mut() {
                let _ = writeln!(f, "{msg}");
            }
        }
    }
}

fn open_log(data_dir: &Path) {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(data_dir.join("splitter-desktop.log"))
        .ok();
    let _ = LOG.set(Mutex::new(file));
}

/// Owns the running sidecar so it dies with the app.
struct Sidecar(Mutex<Option<Child>>);

impl Sidecar {
    fn kill(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
                log("sidecar stopped");
            }
        }
    }
}

pub fn run() {
    // Headless modes for the installer (NSIS hooks, `nsis/hooks.nsh`): no window, no
    // Tauri, exit code only. Everything they say goes to the service data dir's log.
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--install-service") => std::process::exit(service_cli("install")),
        Some("--remove-service") => std::process::exit(service_cli("remove")),
        // The Service Control Manager starts us with this (see service_cli): host the sidecar.
        Some("--service") => std::process::exit(service_host()),
        _ => {}
    }

    tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let data_dir = resolve_data_dir(&handle);
            open_log(&data_dir);
            log(format!(
                "Splitter {} starting — data dir {}",
                env!("SPLITTER_BUILD"),
                data_dir.display()
            ));

            // A server already running here (the Windows service, or another window's
            // sidecar) is the one to show: starting a second would fight it for 8100 and
            // for the game, which feeds only its newest client.
            let mut candidates: Vec<PathBuf> = service_data_dir().into_iter().collect();
            candidates.push(data_dir.clone());
            let (child, url) = match running_node(&candidates) {
                Some(url) => {
                    log(format!("attaching to the Splitter already running at {url}"));
                    (None, url)
                }
                None => {
                    let exe = extract_sidecar(&data_dir)?;
                    let (child, url) = spawn_sidecar(&exe, &data_dir)?;
                    log(format!("sidecar ready — opening {url}"));
                    (Some(child), url)
                }
            };

            WebviewWindowBuilder::new(
                &handle,
                "main",
                WebviewUrl::External(url.parse().expect("sidecar URL is valid")),
            )
            .title("Splitter")
            // Runs before every page's own scripts: base.html reads it to hide the
            // browser-only controls (install, fullscreen) and skip the service worker.
            .initialization_script(format!(
                "window.SPLITTER_DESKTOP = {:?};",
                env!("SPLITTER_BUILD")
            ))
            .inner_size(1180.0, 820.0)
            .min_inner_size(720.0, 520.0)
            .build()?;

            app.manage(Sidecar(Mutex::new(child)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Splitter desktop app")
        .run(|handle, event| {
            if let RunEvent::Exit = event {
                if let Some(s) = handle.try_state::<Sidecar>() {
                    s.kill();
                }
            }
        });
}

/// `--install-service` / `--remove-service` (the installer's hooks, run elevated): unpack the
/// embedded sidecar into the **service** data dir and register it as the Windows service
/// (`splitter-sidecar service install`, which stops and deletes an older registration
/// first so an update re-points the service at the new sidecar), or remove it. Returns
/// the exit code for the hook.
fn service_cli(action: &str) -> i32 {
    let Some(data_dir) = service_data_dir() else {
        eprintln!("splitter-desktop: no service data dir on this platform");
        return 2;
    };
    if fs::create_dir_all(&data_dir).is_err() {
        eprintln!("splitter-desktop: cannot create {}", data_dir.display());
        return 2;
    }
    open_log(&data_dir);
    log(format!("{action} service — data dir {}", data_dir.display()));
    let exe = match extract_sidecar(&data_dir) {
        Ok(exe) => exe,
        Err(e) => {
            log(format!("cannot unpack the sidecar: {e}"));
            return 1;
        }
    };
    if !exe.is_file() {
        // Defender quarantining the freshly unpacked sidecar leaves exactly this.
        log(format!("sidecar is not there after unpacking: {} — antivirus?", exe.display()));
        return 1;
    }
    if action == "install" {
        // Re-registering is the upgrade path: an older service would point at the
        // previous version's folder, which extract_sidecar just removed.
        run_logged(Command::new(&exe).args(["service", "remove"]), "service remove (before install)");
        firewall_rule(&exe, true);
    }
    let mut cmd = Command::new(&exe);
    cmd.arg("service").arg(action);
    if action == "install" {
        // The SERVICE binary is this shell (`--service`), not the Python sidecar: a native
        // process answers the SCM at once, then starts the sidecar however long that takes.
        // service.py appends `--service --data-dir <dir>` to the executable it is given.
        let host = std::env::current_exe().map_err(|e| e.to_string());
        let Ok(host) = host else {
            log("cannot find this executable's own path");
            return 1;
        };
        cmd.arg("--exe").arg(&host).arg("--data-dir").arg(&data_dir);
    }
    let code = run_logged(&mut cmd, &format!("service {action}"));
    if action == "remove" {
        firewall_rule(&exe, false);
    }
    code
}

/// `--service`: the process the Service Control Manager runs. Registers with the SCM
/// immediately (the 30 s start budget is spent here, on a native binary, never on Python
/// start-up — which is what timed out on the gaming PC on 2026-09-27), then unpacks and
/// spawns the sidecar exactly as a window would, restarts it if it dies, and stops it on
/// Stop / Shutdown. The sidecar's output lands in the data dir's splitter-desktop.log.
#[cfg(windows)]
fn service_host() -> i32 {
    service_host::run()
}

#[cfg(not(windows))]
fn service_host() -> i32 {
    eprintln!("splitter-desktop: --service is the Windows service host; use `splitter service install` here");
    2
}

#[cfg(windows)]
mod service_host {
    use super::*;
    use std::ffi::OsString;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use windows_service::service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
    use windows_service::{define_windows_service, service_dispatcher};

    const NAME: &str = "Splitter";

    define_windows_service!(ffi_service_main, service_main);

    pub fn run() -> i32 {
        match service_dispatcher::start(NAME, ffi_service_main) {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("splitter-desktop: not started by the service control manager: {e}");
                1
            }
        }
    }

    fn service_main(_args: Vec<OsString>) {
        if let Err(e) = serve() {
            log(format!("service host failed: {e}"));
        }
    }

    fn status(state: ServiceState) -> ServiceStatus {
        ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
            exit_code: ServiceExitCode::Win32(0),
            checkpoint: 0,
            wait_hint: Duration::default(),
            process_id: None,
        }
    }

    fn sleep_unless_stopping(stopping: &AtomicBool, d: Duration) {
        let end = Instant::now() + d;
        while Instant::now() < end && !stopping.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    fn serve() -> Result<(), Box<dyn std::error::Error>> {
        let data_dir = service_data_dir().ok_or("no service data dir on this platform")?;
        fs::create_dir_all(&data_dir)?;
        open_log(&data_dir);
        let stopping = Arc::new(AtomicBool::new(false));
        let flag = stopping.clone();
        let handle = service_control_handler::register(NAME, move |control| match control {
            ServiceControl::Stop | ServiceControl::Shutdown => {
                flag.store(true, Ordering::SeqCst);
                ServiceControlHandlerResult::NoError
            }
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            _ => ServiceControlHandlerResult::NotImplemented,
        })?;
        // Running NOW: the SCM's clock stops here, before the sidecar's own start-up.
        handle.set_service_status(status(ServiceState::Running))?;
        log(format!(
            "Splitter {} service host running — data dir {}",
            env!("SPLITTER_BUILD"),
            data_dir.display()
        ));

        let mut backoff = Duration::from_secs(2);
        while !stopping.load(Ordering::SeqCst) {
            let started = extract_sidecar(&data_dir).and_then(|exe| spawn_sidecar(&exe, &data_dir));
            let child = match started {
                Ok((child, url)) => {
                    log(format!("sidecar ready at {url}"));
                    backoff = Duration::from_secs(2);
                    child
                }
                Err(e) => {
                    log(format!("sidecar failed to start: {e} — retrying in {backoff:?}"));
                    sleep_unless_stopping(&stopping, backoff);
                    backoff = (backoff * 2).min(Duration::from_secs(60));
                    continue;
                }
            };
            let sidecar = Sidecar(Mutex::new(Some(child)));
            loop {
                if stopping.load(Ordering::SeqCst) {
                    sidecar.kill();
                    // Killed, not asked to exit: the sidecar's own marker cleanup never ran.
                    for marker in ["node.port", "node.pid"] {
                        let _ = fs::remove_file(data_dir.join(marker));
                    }
                    break;
                }
                let exited = match sidecar.0.lock() {
                    Ok(mut guard) => match guard.as_mut() {
                        Some(c) => matches!(c.try_wait(), Ok(Some(_))),
                        None => true,
                    },
                    Err(_) => true,
                };
                if exited {
                    log("sidecar exited on its own — restarting in 5 s");
                    sleep_unless_stopping(&stopping, Duration::from_secs(5));
                    break;
                }
                std::thread::sleep(Duration::from_millis(500));
            }
        }
        handle.set_service_status(status(ServiceState::Stopped))?;
        log("service host stopped");
        Ok(())
    }
}

/// Run a command with no console, logging every line it prints and its exit status —
/// the hooks run this shell headless, so this log is the only place `sc`'s words survive.
fn run_logged(cmd: &mut Command, what: &str) -> i32 {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    match cmd.output() {
        Ok(out) => {
            for line in String::from_utf8_lossy(&out.stdout)
                .lines()
                .chain(String::from_utf8_lossy(&out.stderr).lines())
            {
                if !line.trim().is_empty() {
                    log(format!("[{what}] {}", line.trim_end()));
                }
            }
            log(format!("{what}: {}", out.status));
            out.status.code().unwrap_or(1)
        }
        Err(e) => {
            log(format!("{what} failed to run: {e}"));
            1
        }
    }
}

/// The inbound rule for the service's sidecar on 8100: a service gets no "allow access?"
/// prompt, so without this the tablet on the LAN is silently blocked. Best effort.
fn firewall_rule(exe: &Path, add: bool) {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("netsh");
        cmd.args(["advfirewall", "firewall"]);
        if add {
            cmd.args(["delete", "rule", "name=Splitter"]);
            run_logged(&mut cmd, "firewall (clear old rule)");
            let mut cmd = Command::new("netsh");
            cmd.args(["advfirewall", "firewall", "add", "rule", "name=Splitter", "dir=in", "action=allow", "protocol=TCP", "localport=8100"]);
            cmd.arg(format!("program={}", exe.display()));
            run_logged(&mut cmd, "firewall (allow 8100)");
        } else {
            cmd.args(["delete", "rule", "name=Splitter"]);
            run_logged(&mut cmd, "firewall (remove rule)");
        }
    }
    #[cfg(not(windows))]
    {
        let _ = (exe, add);
    }
}

/// Where a system-service install keeps its state — the same answer as
/// `splitter/service.py::default_data_dir`, which is what `splitter service install` uses.
fn service_data_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        std::env::var_os("PROGRAMDATA").map(|p| PathBuf::from(p).join("Splitter"))
    }
    #[cfg(target_os = "macos")]
    {
        Some(PathBuf::from("/Library/Application Support/Splitter"))
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Some(PathBuf::from("/var/lib/splitter"))
    }
}

/// A Splitter already running on this machine: the first data dir whose `node.port` names a
/// port that answers `/healthz`. The sidecar writes the marker once bound and removes it on
/// a clean exit; a marker left by a crash names a dead port, which the probe rejects.
fn running_node(dirs: &[PathBuf]) -> Option<String> {
    for dir in dirs {
        let Ok(text) = fs::read_to_string(dir.join("node.port")) else { continue };
        let Ok(port) = text.trim().parse::<u16>() else { continue };
        if healthz_ok(port) {
            return Some(format!("http://127.0.0.1:{port}/"));
        }
        log(format!("stale node.port in {} (port {port} is not answering)", dir.display()));
    }
    None
}

/// `GET /healthz` on loopback with short timeouts — plain HTTP/1.0 over a socket, so the
/// shell needs no HTTP client crate for one request at startup.
fn healthz_ok(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(800)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
    if s.write_all(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);
    text.starts_with("HTTP/1.") && text.contains(" 200 ") && text.contains("\"ok\":true")
}

/// `splitter-data/` beside the executable when writable, else the per-user app-data dir.
fn resolve_data_dir(handle: &tauri::AppHandle) -> PathBuf {
    if let Some(dir) = portable_data_dir() {
        return dir;
    }
    let dir = handle
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("splitter"));
    let _ = fs::create_dir_all(&dir);
    dir
}

fn portable_data_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?.join("splitter-data");
    fs::create_dir_all(&dir).ok()?;
    let marker = dir.join(".write-test");
    match File::create(&marker) {
        Ok(_) => {
            let _ = fs::remove_file(&marker);
            Some(dir)
        }
        Err(_) => None,
    }
}

/// Unpack the embedded sidecar archive into `<data>/bin/splitter-sidecar-<version>/` if
/// it is not there yet (or was unpacked from a different archive), drop folders and
/// files left by other versions, and return the path of the executable inside it.
fn extract_sidecar(data_dir: &Path) -> Result<PathBuf, Box<dyn std::error::Error>> {
    if !SIDECAR_EMBEDDED || SIDECAR.is_empty() {
        return Err("this build has no embedded sidecar (built with SPLITTER_ALLOW_EMPTY_SIDECAR)".into());
    }
    let bin_dir = data_dir.join("bin");
    fs::create_dir_all(&bin_dir)?;
    let dir_name = format!("{SIDECAR_NAME}-{}", env!("SPLITTER_BUILD"));
    let dir = bin_dir.join(&dir_name);
    let exe_name = if cfg!(windows) { "splitter-sidecar.exe" } else { "splitter-sidecar" };
    let exe = dir.join(exe_name);
    let stamp = SIDECAR.len().to_string();

    let up_to_date = fs::read_to_string(dir.join(SIDECAR_MARKER))
        .map(|s| s.trim() == stamp)
        .unwrap_or(false)
        && exe.is_file();
    if !up_to_date {
        log(format!("unpacking sidecar ({} MB) to {}", SIDECAR.len() / 1_000_000, dir.display()));
        let tmp = bin_dir.join(format!("{dir_name}.part"));
        let _ = fs::remove_dir_all(&tmp);
        fs::create_dir_all(&tmp)?;
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(SIDECAR));
        archive.set_preserve_permissions(true);
        archive.set_overwrite(true);
        for entry in archive.entries()? {
            let mut entry = entry?;
            let path = entry.path()?.into_owned();
            // Every entry is under a top-level `splitter-sidecar/`; unpack it flat into tmp.
            let rel = path
                .strip_prefix(SIDECAR_NAME)
                .map_err(|_| format!("unexpected entry in sidecar archive: {}", path.display()))?
                .to_path_buf();
            if rel.as_os_str().is_empty() {
                continue;
            }
            let dst = tmp.join(rel);
            if let Some(parent) = dst.parent() {
                fs::create_dir_all(parent)?; // the archive has file entries only
            }
            entry.unpack(&dst)?;
        }
        if !tmp.join(exe_name).is_file() {
            return Err("sidecar archive has no executable in it".into());
        }
        fs::write(tmp.join(SIDECAR_MARKER), &stamp)?;
        let _ = fs::remove_dir_all(&dir);
        fs::rename(&tmp, &dir)?;
    }

    // Older versions (one-dir folders, or the single exes of pre-0.5.2 builds) only
    // take up space in a portable install; the current one is all that runs.
    if let Ok(entries) = fs::read_dir(&bin_dir) {
        for e in entries.flatten() {
            let name = e.file_name();
            let name = name.to_string_lossy();
            if name.starts_with(SIDECAR_NAME) && name != dir_name {
                let p = e.path();
                let removed = if p.is_dir() { fs::remove_dir_all(&p) } else { fs::remove_file(&p) };
                if removed.is_ok() {
                    log(format!("removed old sidecar {}", p.display()));
                }
            }
        }
    }
    Ok(exe)
}

/// Start the sidecar and wait for its `SPLITTER_READY <url>` line.
fn spawn_sidecar(exe: &Path, data_dir: &Path) -> Result<(Child, String), Box<dyn std::error::Error>> {
    let mut cmd = Command::new(exe);
    cmd.arg("--data-dir")
        .arg(data_dir)
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .env("SPLITTER_DATA_DIR", data_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: runs in the forked child before exec; only calls an async-signal-safe
        // syscall. Makes the kernel deliver SIGTERM to the sidecar if this process dies.
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                Ok(())
            });
        }
    }
    let mut child = cmd.spawn().map_err(|e| format!("cannot start sidecar {}: {e}", exe.display()))?;

    // Mirror the sidecar's stderr (uvicorn / app logs) into our log.
    if let Some(err) = child.stderr.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                log(format!("[sidecar] {line}"));
            }
        });
    }
    let stdout = child.stdout.take().ok_or("sidecar stdout not captured")?;
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(url) = line.strip_prefix("SPLITTER_READY ") {
                let _ = tx.send(url.trim().to_string());
            } else {
                log(format!("[sidecar] {line}"));
            }
        }
    });

    let started = Instant::now();
    loop {
        match rx.recv_timeout(Duration::from_millis(250)) {
            Ok(url) => return Ok((child, url)),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("sidecar exited before it was ready ({status}) — see the log").into());
        }
        if started.elapsed() > READY_TIMEOUT {
            let _ = child.kill();
            return Err("sidecar did not report ready within 60 s — see the log".into());
        }
    }
    Err("sidecar closed its output before reporting ready".into())
}
