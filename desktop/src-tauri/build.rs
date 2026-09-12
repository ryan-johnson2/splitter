//! Embed the PyInstaller sidecar into the shell binary.
//!
//! `SPLITTER_SIDECAR` (env) names the sidecar executable to embed; it defaults to
//! `../sidecar/dist/splitter-sidecar[.exe]`. The file is copied into `OUT_DIR` as
//! `sidecar.bin` and pulled in by `include_bytes!` in `lib.rs`. A missing sidecar
//! is a hard error at build time — better than shipping an empty shell — unless
//! `SPLITTER_ALLOW_EMPTY_SIDECAR=1` (for `cargo check` without a Python build).

use std::path::PathBuf;

fn main() {
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
