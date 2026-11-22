//! Localhost Sync Server (`127.0.0.1:<port>`) matching `src/server.py` 100%.
//!
//! Invariants:
//! - Lossless multi-tab `/poll?since=<seq>` backed by monotonic `_event_seq` + durable
//!   `_event_log` ring buffer (`maxlen = 128`) + separate per-type `_ephemeral_events`.
//! - Request body limit >= 32 MiB for `/cross_map_export`.
//! - `/shutdown` enforces `POST` + loopback (`403 Forbidden`, never `405`).

pub const EVENT_LOG_MAXLEN: usize = 128;
pub const EVENT_QUEUE_MAXLEN: usize = 64;
pub const POLL_TIMEOUT_SECS: u64 = 15;
pub const MAX_HTTP_BODY_BYTES: usize = 32 * 1024 * 1024;
