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
