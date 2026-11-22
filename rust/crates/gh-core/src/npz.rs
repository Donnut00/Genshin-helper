//! Pure-Rust `.npy`/`.npz` reader & writer supporting the 4 C-contiguous LE dtypes
//! used by Genshin-Helper datasets (`u8`, `i32`, `i64`, `f32`).
//! Phase 2 target: replaces NumPy `np.load` / `np.savez_compressed`.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NpyDType {
    U8,
    I32,
    I64,
    F32,
}
