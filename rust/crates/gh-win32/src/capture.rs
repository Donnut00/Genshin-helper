//! Flicker-free GDI `BitBlt` capture (`src/capture.py`).
//!
//! DECISION: Always use plain `SRCCOPY` (`0x00CC_0020`), NEVER `CAPTUREBLT`, with a
//! thread-local 4-slot LRU top-down 32-bit `CreateDIBSection` pool.

pub const BITBLT_ROP_SRCCOPY: u32 = 0x00CC_0020;
pub const DIB_POOL_SLOTS: usize = 4;
