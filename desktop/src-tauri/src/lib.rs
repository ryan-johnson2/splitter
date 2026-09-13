//! Splitter native desktop app (Tauri 2) — the **portable, standalone** release form.
//!
//! The Python app is untouched; this shell is packaging, in GridFPV's shape:
//!
//! 1. Resolve a **true-portable data dir**: `splitter-data/` beside the running executable
//!    when that is writable, else the per-user app-data dir. The SQLite database, the
//!    extracted sidecar and the log file all live there, so a copied exe carries its data.
//! 2. Extract the **embedded sidecar** (the PyInstaller build of the Python server, baked
//!    into this binary at compile time by `build.rs`) into `<data>/bin/`, once per version.
//! 3. Spawn it with `SPLITTER_DATA_DIR` set, read its stdout until it prints
//!    `SPLITTER_READY <url>`, and open the main window at that URL. The sidecar binds all
//!    interfaces on 8100 (fallback: ephemeral) so a tablet on the LAN can open the same
//!    pages; the window itself always uses loopback.
//! 4. Kill the sidecar when the app exits.
//!
//! Diagnostics: release builds on Windows have no console (`main.rs`), so everything
//! this shell wants to say goes to `<data>/splitter-desktop.log` as well as stderr.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

/// The sidecar executable, embedded at build time (see `build.rs`).
static SIDECAR: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/sidecar.bin"));
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

            let exe = extract_sidecar(&data_dir)?;
            let (child, url) = spawn_sidecar(&exe, &data_dir)?;
            log(format!("sidecar ready — opening {url}"));

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

            app.manage(Sidecar(Mutex::new(Some(child))));
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

/// Write the embedded sidecar to `<data>/bin/splitter-sidecar-<version>[.exe]` if it is
/// not there yet (or has a different size), and make it executable.
fn extract_sidecar(data_dir: &Path) -> Result<PathBuf, Box<dyn std::error::Error>> {
    if !SIDECAR_EMBEDDED || SIDECAR.is_empty() {
        return Err("this build has no embedded sidecar (built with SPLITTER_ALLOW_EMPTY_SIDECAR)".into());
    }
    let bin_dir = data_dir.join("bin");
    fs::create_dir_all(&bin_dir)?;
    let name = format!(
        "splitter-sidecar-{}{}",
        env!("SPLITTER_BUILD"),
        if cfg!(windows) { ".exe" } else { "" }
    );
    let path = bin_dir.join(name);
    let up_to_date = fs::metadata(&path)
        .map(|m| m.len() == SIDECAR.len() as u64)
        .unwrap_or(false);
    if !up_to_date {
        log(format!("extracting sidecar ({} MB) to {}", SIDECAR.len() / 1_000_000, path.display()));
        let tmp = path.with_extension("part");
        fs::write(&tmp, SIDECAR)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o755))?;
        }
        fs::rename(&tmp, &path)?;
    }
    Ok(path)
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
