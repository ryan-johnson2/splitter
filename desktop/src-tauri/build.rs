//! Embed the PyInstaller sidecar into the shell binary.
//!
//! `SPLITTER_SIDECAR` (env) names the sidecar executable to embed; it defaults to
//! `../sidecar/dist/splitter-sidecar[.exe]`. The file is copied into `OUT_DIR` as
//! `sidecar.bin` and pulled in by `include_bytes!` in `lib.rs`. A missing sidecar
//! is a hard error at build time — better than shipping an empty shell — unless
//! `SPLITTER_ALLOW_EMPTY_SIDECAR=1` (for `cargo check` without a Python build).

use std::path::PathBuf;
use std::process::Command;

/// Same rule as scripts/stamp.py: SPLITTER_BUILD env, else a clean v* tag, else
/// <pkg>-dev-<hash>[-dirty], else the package version.
fn build_stamp(pkg: &str) -> String {
    if let Ok(v) = std::env::var("SPLITTER_BUILD") {
        if !v.trim().is_empty() {
            return v.trim().trim_start_matches('v').to_string();
        }
    }
    let dirty = git(&["status", "--porcelain"]).is_some();
    if !dirty {
        if let Some(tag) = git(&["describe", "--exact-match", "--tags", "--match", "v*", "HEAD"]) {
            return tag.trim_start_matches('v').to_string();
        }
    }
    let Some(hash) = git(&["rev-parse", "--short=7", "HEAD"]) else {
        return pkg.to_string();
    };
    let base = pkg.split('-').next().unwrap_or(pkg);
    format!("{base}-dev-{hash}{}", if dirty { "-dirty" } else { "" })
}

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8(out.stdout).ok()?;
    let s = s.trim();
    (!s.is_empty()).then(|| s.to_string())
}

fn main() {
    println!("cargo:rerun-if-env-changed=SPLITTER_BUILD");
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/index");
    let pkg = std::env::var("CARGO_PKG_VERSION").expect("CARGO_PKG_VERSION");
    println!("cargo:rustc-env=SPLITTER_BUILD={}", build_stamp(&pkg));

    let out = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));
    let default = {
        let name = if cfg!(windows) { "splitter-sidecar.exe" } else { "splitter-sidecar" };
        PathBuf::from("../sidecar/dist").join(name)
    };
    let src = std::env::var_os("SPLITTER_SIDECAR").map(PathBuf::from).unwrap_or(default);
    println!("cargo:rerun-if-env-changed=SPLITTER_SIDECAR");
    println!("cargo:rerun-if-env-changed=SPLITTER_ALLOW_EMPTY_SIDECAR");
    println!("cargo:rerun-if-changed={}", src.display());
    let dest = out.join("sidecar.bin");
    if src.is_file() {
        std::fs::copy(&src, &dest).expect("copy sidecar into OUT_DIR");
        println!("cargo:rustc-env=SPLITTER_SIDECAR_EMBEDDED=1");
    } else if std::env::var("SPLITTER_ALLOW_EMPTY_SIDECAR").as_deref() == Ok("1") {
        std::fs::write(&dest, b"").expect("write empty sidecar placeholder");
        println!("cargo:rustc-env=SPLITTER_SIDECAR_EMBEDDED=0");
        println!("cargo:warning=building WITHOUT an embedded sidecar (SPLITTER_ALLOW_EMPTY_SIDECAR=1)");
    } else {
        panic!(
            "sidecar not found at {} — run `python desktop/sidecar/build.py` first, \
             or set SPLITTER_SIDECAR to the executable",
            src.display()
        );
    }
    tauri_build::build();
}
