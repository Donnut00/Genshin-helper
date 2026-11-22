//! Standalone minimal Chrome/Edge Native Messaging Host (`com.genshinhelper.host`).
//! Console subsystem is REQUIRED for 4-byte little-endian stdio framing (`src/native_host.py`).

use gh_core::{HOST_VERSION, NATIVE_HOST_NAME};

fn main() {
    // Phase 2 target: implement 4-byte LE stdin/stdout loop (`ping`, `start_server`, `stop_server`).
    // Never write diagnostic logs to stdout (corrupts Chrome Native Messaging frames).
    let _ = (HOST_VERSION, NATIVE_HOST_NAME);
}
