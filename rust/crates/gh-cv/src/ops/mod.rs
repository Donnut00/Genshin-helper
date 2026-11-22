//! Pure-Rust CV primitives: color, resize, Sobel/Scharr, FFT & Padmanabhan (2012) masked 6-FFT NCC,
//! phase correlation, warp polar/affine, contours/Canny/CLAHE, RootSIFT, 4-tree KD-Tree, ORB, RANSAC.

use gh_core::frame::FrameView;

/// Internal feature extraction/matching boundary allowing Phase 3 Gate 0 tuning
/// without changing `locator.rs`, `global_relocator.rs`, or `minimap_tracker.rs`.
pub trait FeatureMatcher: Send + Sync {
    fn extract_keypoints_and_descriptors(&self, gray: FrameView<'_>) -> Vec<([f32; 2], Vec<f32>)>;
}
