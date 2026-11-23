//! Hybrid minimap local tracker, 4-state FSM, 2-zoom/floor hysteresis, Paimon auto-bootstrap,
//! Follow-View `-theta_mm_to_ref`, bounded `phaseCorrelate`, and daemon worker (`src/minimap_tracker.py`).

use crate::global_relocator::{TeyvatGlobalRelocator, VoteBins};
use crate::heading360::{
    CameraHeadingFuser, CircularAngleSmoother, FuserSource, extract_camera_view_cone,
    extract_player_arrow_360, shortest_angle_delta, wrap_angle_360,
};
use crate::image_io::DecodedImage;
use crate::ops::color::{bgr_px_to_gray, bgr_to_gray, bgr_to_hsv, frame_view_to_array3};
use crate::ops::filters::clahe_u8;
use crate::ops::flann::{bf_knn_match_l2, lowe_ratio_filter};
use crate::ops::morph::{
    draw_filled_circle_f32, draw_filled_ellipse_sector_f32, draw_filled_ellipse_sector_u8,
};
use crate::ops::ncc::{
    match_template_f32, match_template_masked_f32, match_template_masked_u8, min_max_loc_f32,
};
use crate::ops::orb::{OrbExtractor, OrbParams, bf_knn_match_hamming};
use crate::ops::phase_correlate::{create_hanning_window, phase_correlate};
use crate::ops::ransac_similarity::estimate_affine_partial_2d_ransac;
use crate::ops::resize::{resize_f32, resize_u8, resize_u8_hwc};
use crate::ops::sift::SiftExtractor;
use crate::ops::sobel::sobel_magnitude_u8_f32;
use crate::ops::warp::{get_rotation_matrix_2d, invert_affine_transform, warp_affine_u8};
use crate::ops::{BorderMode, CvError, CvResult, Interpolation, KeyPoint, TemplateMatchMode};
use crate::tile_cache::{
    LruTerrainTileCache, OverlayBounds, extract_underground_patch_gray, overlay_bytes_to_gray,
};
use gh_core::frame::{FrameView, PixelFormat, RectI32};
use gh_core::multimap::{FloorOverlayRecord, MultimapDb};
use gh_core::npz::{NpyArray, NpyData, NpzArchive};
use gh_core::traits::UndergroundOverlaySource;
use gh_core::ui_layout::{UiLayout, compute_ui_layout, detect_hdr_clipping};
use ndarray::{Array2, Array3, ArrayView2, ArrayView3, s};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

pub const STATE_LOST: &str = "LOST";
pub const STATE_ACQUIRING: &str = "ACQUIRING";
pub const STATE_TRACKING: &str = "TRACKING";
pub const STATE_RELOCATING: &str = "RELOCATING";

pub const MODE_LOCK_DIRECTION: &str = "lock_direction";
pub const MODE_FOLLOW_VIEW: &str = "follow_view";

pub const ZOOM_OPEN_WORLD: &str = "open_world";
pub const ZOOM_CITY: &str = "city";

pub type GlobalLocatorFn =
    Box<dyn Fn(ArrayView3<'_, u8>, Option<i32>) -> Option<(i32, f64, f64, f64)> + Send + Sync>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
pub enum TrackerState {
    #[default]
    Lost,
    Acquiring,
    Tracking,
    Relocating,
}

impl TrackerState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Lost => STATE_LOST,
            Self::Acquiring => STATE_ACQUIRING,
            Self::Tracking => STATE_TRACKING,
            Self::Relocating => STATE_RELOCATING,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
pub enum MinimapMode {
    #[default]
    LockDirection,
    FollowView,
}

impl MinimapMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::LockDirection => MODE_LOCK_DIRECTION,
            Self::FollowView => MODE_FOLLOW_VIEW,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
pub enum ZoomRegime {
    #[default]
    OpenWorld,
    City,
}

impl ZoomRegime {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::OpenWorld => ZOOM_OPEN_WORLD,
            Self::City => ZOOM_CITY,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct MinimapTrackerConfig {
    pub min_confidence: f64,
    pub acquire_hits: usize,
    pub relocate_hits: usize,
    pub candidate_radius_world: f64,
    pub max_speed_world_per_second: f64,
    pub jump_margin_world: f64,
    pub smoothing_alpha: f64,
    pub lost_timeout_seconds: f64,

    pub open_world_span_world: float_or_f64!(),
    pub city_span_world: f64,
    pub open_world_patch_px_per_unit: f64,
    pub city_patch_px_per_unit: f64,
    pub search_radius_open_world: f64,
    pub search_radius_city: f64,
    pub match_downscale: f64,

    pub annulus_inner_ratio: f64,
    pub annulus_outer_ratio: f64,

    pub zoom_switch_min_corr: f64,
    pub zoom_switch_margin: f64,
    pub zoom_switch_consecutive_wins: usize,

    pub follow_view_cam_pin_deg: f64,
    pub follow_view_unlock_deg: f64,
    pub follow_view_min_map_rot_deg: f64,
    pub follow_view_min_inliers: usize,

    pub template_min_corr: f64,
    pub phase_min_response: f64,
    pub phase_max_shift_frac: f64,
    pub max_phase_fallback_frames: usize,

    pub floor_switch_margin: f64,
    pub floor_switch_min_score: f64,
    pub floor_switch_consecutive_wins: usize,

    pub gate_min_std: f64,
    pub gate_stationary_diff: f64,
    pub gate_cone_change_diff: f64,
    pub gate_border_coherence: f64,

    pub sift_min_inliers: usize,
    pub sobel_min_corr: f64,
    pub paimon_thresh: f64,
    pub broadcast_min_sift_inliers: usize,
    pub broadcast_min_sobel_corr: f64,
    pub enable_z0_city: bool,
    pub enable_auto_layer: bool,
    pub enable_relocating_guard: bool,
    pub enable_interframe_sobel: bool,
    pub interframe_sobel_k: usize,
    pub interframe_sobel_min_corr: f64,
    pub enable_global_relocate: bool,
    pub hold_on_phase_exhausted: bool,
    pub arrow_gate_change_diff: f64,
}

macro_rules! float_or_f64 {
    () => {
        f64
    };
}
use float_or_f64;

impl Default for MinimapTrackerConfig {
    fn default() -> Self {
        Self {
            min_confidence: 0.45,
            acquire_hits: 2,
            relocate_hits: 3,
            candidate_radius_world: 35.0,
            max_speed_world_per_second: 120.0,
            jump_margin_world: 25.0,
            smoothing_alpha: 0.4,
            lost_timeout_seconds: 1.5,

            open_world_span_world: 360.0,
            city_span_world: 120.0,
            open_world_patch_px_per_unit: 0.5,
            city_patch_px_per_unit: 1.0,
            search_radius_open_world: 96.0,
            search_radius_city: 48.0,
            match_downscale: 0.5,

            annulus_inner_ratio: 0.20,
            annulus_outer_ratio: 0.85,

            zoom_switch_min_corr: 0.72,
            zoom_switch_margin: 0.08,
            zoom_switch_consecutive_wins: 2,

            follow_view_cam_pin_deg: 12.0,
            follow_view_unlock_deg: 16.0,
            follow_view_min_map_rot_deg: 8.0,
            follow_view_min_inliers: 8,

            template_min_corr: 0.45,
            phase_min_response: 0.15,
            phase_max_shift_frac: 0.25,
            max_phase_fallback_frames: 5,

            floor_switch_margin: 0.08,
            floor_switch_min_score: 0.25,
            floor_switch_consecutive_wins: 3,

            gate_min_std: 12.0,
            gate_stationary_diff: 1.2,
            gate_cone_change_diff: 0.15,
            gate_border_coherence: 0.42,

            sift_min_inliers: 6,
            sobel_min_corr: 0.34,
            paimon_thresh: 0.56,
            broadcast_min_sift_inliers: 6,
            broadcast_min_sobel_corr: 0.38,
            enable_z0_city: true,
            enable_auto_layer: true,
            enable_relocating_guard: true,
            enable_interframe_sobel: true,
            interframe_sobel_k: 3,
            interframe_sobel_min_corr: 0.38,
            enable_global_relocate: true,
            hold_on_phase_exhausted: false,
            arrow_gate_change_diff: 1.8,
        }
    }
}

impl MinimapTrackerConfig {
    pub fn production_defaults() -> Self {
        Self {
            gate_border_coherence: 0.22,
            template_min_corr: 0.34,
            min_confidence: 0.34,
            relocate_hits: 2,
            candidate_radius_world: 40.0,
            lost_timeout_seconds: 12.0,
            max_phase_fallback_frames: 12,
            max_speed_world_per_second: 135.0,
            jump_margin_world: 30.0,
            smoothing_alpha: 0.55,
            hold_on_phase_exhausted: true,
            ..Self::default()
        }
    }

    pub fn validate(&self) -> CvResult<()> {
        if !(self.smoothing_alpha > 0.0 && self.smoothing_alpha <= 1.0) {
            return Err(CvError::InvalidArgument(format!(
                "smoothing_alpha must be in (0, 1], got {}",
                self.smoothing_alpha
            )));
        }
        if self.acquire_hits < 1 || self.relocate_hits < 1 {
            return Err(CvError::InvalidArgument(
                "acquire_hits and relocate_hits must be >= 1".into(),
            ));
        }
        if self.lost_timeout_seconds <= 0.0 || !self.lost_timeout_seconds.is_finite() {
            return Err(CvError::InvalidArgument(
                "lost_timeout_seconds must be > 0".into(),
            ));
        }
        if self.max_phase_fallback_frames < 1 {
            return Err(CvError::InvalidArgument(
                "max_phase_fallback_frames must be >= 1".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MinimapGateResult {
    pub valid: bool,
    pub stationary: bool,
    pub frame_diff: f64,
    pub alpha_diff: f64,
    pub center_diff: f64,
    pub inner_std: f64,
    pub border_coherence: f64,
    pub hdr_clipped: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PoseMeasurement {
    pub found: bool,
    pub map_id: Option<i32>,
    pub world_x: Option<f64>,
    pub world_y: Option<f64>,
    pub confidence: f64,
    pub is_underground: bool,
    pub floor_id: Option<i32>,
    pub floor_name: String,
    pub match_method: String,
    pub reason: Option<String>,
}

impl Default for PoseMeasurement {
    fn default() -> Self {
        Self {
            found: false,
            map_id: None,
            world_x: None,
            world_y: None,
            confidence: 0.0,
            is_underground: false,
            floor_id: None,
            floor_name: "G".to_owned(),
            match_method: "none".to_owned(),
            reason: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MinimapPose {
    pub state: TrackerState,
    pub map_id: Option<i32>,
    pub world_x: Option<f64>,
    pub world_y: Option<f64>,
    pub lat: Option<f64>,
    pub lng: Option<f64>,
    pub camera_heading: Option<f64>,
    pub body_heading: Option<f64>,
    pub view_heading: Option<f64>,
    pub minimap_mode: MinimapMode,
    pub zoom_regime: ZoomRegime,
    pub is_underground: bool,
    pub floor_id: Option<i32>,
    pub confidence: f64,
    pub timestamp: f64,
    pub hdr_clipped: bool,
    pub floor_name: String,
    pub stationary: bool,
    pub match_method: String,
    pub map_rotation_deg: f64,
    pub reason: Option<String>,
    pub should_broadcast_web: bool,
    pub span_world: f64,
    pub paimon_score: float_or_f64!(),
    pub sift_inliers: usize,
    pub zoom_tier: i32,
    pub cal_dx_px: i32,
    pub cal_dy_px: i32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FloorCompareResult {
    pub surface_score: f64,
    pub underground_score: f64,
    pub margin: f64,
    pub is_underground: bool,
    pub floor_id: Option<i32>,
    pub floor_name: String,
    pub streak: usize,
    pub switched: bool,
}

pub struct UndergroundOverlayInput<'a> {
    pub image: &'a DecodedImage,
    pub bounds: OverlayBounds,
    pub floor_id: Option<i32>,
    pub floor_name: &'a str,
}

fn mean_f64_u8(vals: &[u8]) -> f64 {
    if vals.is_empty() {
        return 0.0;
    }
    let sum: u64 = vals.iter().map(|&v| v as u64).sum();
    sum as f64 / vals.len() as f64
}

fn std_f64_u8(vals: &[u8]) -> f64 {
    if vals.is_empty() {
        return 0.0;
    }
    let mut s = 0u64;
    let mut s2 = 0u64;
    for &v in vals {
        let x = v as u64;
        s += x;
        s2 += x * x;
    }
    let n = vals.len() as f64;
    let m = s as f64 / n;
    (s2 as f64 / n - m * m).max(0.0).sqrt()
}

fn std_f64_f32(vals: &[f32]) -> f64 {
    if vals.is_empty() {
        return 0.0;
    }
    let mut s = 0.0f64;
    let mut s2 = 0.0f64;
    for &v in vals {
        let x = v as f64;
        s += x;
        s2 += x * x;
    }
    let n = vals.len() as f64;
    let m = s / n;
    (s2 / n - m * m).max(0.0).sqrt()
}

fn mean_abs_diff_u8(a: &[u8], b: &[u8]) -> f64 {
    if a.is_empty() || a.len() != b.len() {
        return 255.0;
    }
    let mut sum = 0u64;
    for (&x, &y) in a.iter().zip(b.iter()) {
        sum += x.abs_diff(y) as u64;
    }
    sum as f64 / a.len() as f64
}

fn median_i32_rounded(vals: &[i32]) -> i32 {
    if vals.is_empty() {
        return 0;
    }
    let mut sorted = vals.to_vec();
    sorted.sort_unstable();
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        let avg = (sorted[n / 2 - 1] as f64 + sorted[n / 2] as f64) * 0.5;
        avg.round_ties_even() as i32
    }
}

fn round_usize_even(val: f64) -> usize {
    (val.round_ties_even() as isize).max(0) as usize
}

pub struct MinimapGate {
    pub min_std: f64,
    pub stationary_diff: f64,
    pub border_coherence: f64,
    pub require_circular_border: bool,
    disk_indices: Vec<usize>,
    center_indices: Vec<usize>,
    annulus_indices: Vec<usize>,
    annulus_angles_deg: Vec<f32>,
    ring_indices: Vec<usize>,
    keyframe_annulus: Option<Vec<u8>>,
    keyframe_alpha: Option<Vec<u8>>,
    keyframe_center: Option<Vec<u8>>,
    keyframe_cam_deg: Option<f64>,
    last_annulus: Option<Vec<u8>>,
}

impl Default for MinimapGate {
    fn default() -> Self {
        Self::new(12.0, 1.2, 0.42, true)
    }
}

impl MinimapGate {
    const SZ: usize = 64;

    pub fn new(
        min_std: f64,
        stationary_diff: f64,
        border_coherence: f64,
        require_circular_border: bool,
    ) -> Self {
        let sz = Self::SZ;
        let c = (sz - 1) as f64 * 0.5;
        let r_max = sz as f64 * 0.5;
        let mut disk_indices = Vec::new();
        let mut center_indices = Vec::new();
        let mut annulus_indices = Vec::new();
        let mut annulus_angles_deg = Vec::new();

        for y in 0..sz {
            let dy = y as f64 - c;
            for x in 0..sz {
                let dx = x as f64 - c;
                let rr = dx.hypot(dy);
                let idx = y * sz + x;
                if rr <= 0.85 * r_max {
                    disk_indices.push(idx);
                }
                if rr <= 0.24 * r_max {
                    center_indices.push(idx);
                }
                if rr > 0.20 * r_max && rr <= 0.85 * r_max {
                    annulus_indices.push(idx);
                    let ang = dx.atan2(-dy).to_degrees().rem_euclid(360.0) as f32;
                    annulus_angles_deg.push(ang);
                }
            }
        }

        let mut ring_indices = Vec::with_capacity(64 * 13);
        let r0 = 0.75 * r_max;
        let r1 = 0.99 * r_max;
        for t_idx in 0..64 {
            let theta = 2.0 * std::f64::consts::PI * (t_idx as f64) / 64.0;
            let ct = theta.cos();
            let st = theta.sin();
            for r_idx in 0..13 {
                let r = r0 + (r1 - r0) * (r_idx as f64) / 12.0;
                let rx = ((c + ct * r).round_ties_even() as isize).clamp(0, (sz - 1) as isize)
                    as usize;
                let ry = ((c + st * r).round_ties_even() as isize).clamp(0, (sz - 1) as isize)
                    as usize;
                ring_indices.push(ry * sz + rx);
            }
        }

        Self {
            min_std,
            stationary_diff,
            border_coherence,
            require_circular_border,
            disk_indices,
            center_indices,
            annulus_indices,
            annulus_angles_deg,
            ring_indices,
            keyframe_annulus: None,
            keyframe_alpha: None,
            keyframe_center: None,
            keyframe_cam_deg: None,
            last_annulus: None,
        }
    }

    pub fn reset_keyframe(&mut self) {
        self.keyframe_annulus = None;
        self.keyframe_alpha = None;
        self.keyframe_center = None;
        self.keyframe_cam_deg = None;
        self.last_annulus = None;
    }

    fn downscale_64_array3(img: ArrayView3<'_, u8>) -> Option<(Array2<u8>, Option<Array2<u8>>)> {
        let (h, w, c) = img.dim();
        if h == 0 || w == 0 {
            return None;
        }
        let sz = Self::SZ;
        if c == 1 {
            let gray = resize_u8(img.slice(s![.., .., 0]), sz, sz, Interpolation::Linear).ok()?;
            return Some((gray, None));
        }
        if c == 3 || c == 4 {
            let small = resize_u8_hwc(img, sz, sz, Interpolation::Linear).ok()?;
            let gray = bgr_to_gray(small.view()).ok()?;
            let alpha = if c == 4 {
                Some(small.slice(s![.., .., 3]).to_owned())
            } else {
                None
            };
            return Some((gray, alpha));
        }
        None
    }

    fn downscale_64_frame(view: FrameView<'_>) -> Option<(Array2<u8>, Option<Array2<u8>>)> {
        let w = view.width as usize;
        let h = view.height as usize;
        if w == 0 || h == 0 {
            return None;
        }
        let sz = Self::SZ;
        match view.format {
            PixelFormat::Gray8 => {
                let mut g = Array2::<u8>::zeros((h, w));
                for y in 0..h {
                    let row = &view.data[y * view.stride..y * view.stride + w];
                    g.row_mut(y).as_slice_mut().unwrap().copy_from_slice(row);
                }
                let gray = resize_u8(g.view(), sz, sz, Interpolation::Linear).ok()?;
                Some((gray, None))
            }
            PixelFormat::Bgr8 | PixelFormat::Bgra8 => {
                let arr = frame_view_to_array3(view).ok()?;
                Self::downscale_64_array3(arr.view())
            }
            PixelFormat::Rgba8 => {
                let arr = frame_view_to_array3(view).ok()?;
                let small = resize_u8_hwc(arr.view(), sz, sz, Interpolation::Linear).ok()?;
                let mut gray = Array2::<u8>::zeros((sz, sz));
                let mut alpha = Array2::<u8>::zeros((sz, sz));
                for y in 0..sz {
                    for x in 0..sz {
                        gray[[y, x]] =
                            bgr_px_to_gray(small[[y, x, 2]], small[[y, x, 1]], small[[y, x, 0]]);
                        alpha[[y, x]] = small[[y, x, 3]];
                    }
                }
                Some((gray, Some(alpha)))
            }
        }
    }

    fn downscale_64_gray(img: ArrayView2<'_, u8>) -> Option<(Array2<u8>, Option<Array2<u8>>)> {
        let (h, w) = img.dim();
        if h == 0 || w == 0 {
            return None;
        }
        let gray = resize_u8(img, Self::SZ, Self::SZ, Interpolation::Linear).ok()?;
        Some((gray, None))
    }

    fn circular_border_coherence(&self, gray64: &[u8], inner_std: f64) -> f64 {
        let thresh = 10.0f64.max(0.35 * inner_std) as f32;
        let mut pos_counts = [0usize; 11];
        let mut neg_counts = [0usize; 11];
        for t in 0..64 {
            let base = t * 13;
            for r in 0..11 {
                let v0 = gray64[self.ring_indices[base + r]] as f32;
                let v2 = gray64[self.ring_indices[base + r + 2]] as f32;
                let d = v2 - v0;
                if d >= thresh {
                    pos_counts[r] += 1;
                } else if d <= -thresh {
                    neg_counts[r] += 1;
                }
            }
        }
        let mut max_net = 0.0f64;
        for r in 0..11 {
            let p = pos_counts[r] as f64 / 64.0;
            let n = neg_counts[r] as f64 / 64.0;
            max_net = max_net.max((p - n).abs());
        }
        max_net
    }

    fn check_downscaled(
        &mut self,
        h: usize,
        w: usize,
        gray64: Array2<u8>,
        alpha64: Option<Array2<u8>>,
        update_keyframe: bool,
    ) -> MinimapGateResult {
        if h.min(w) < 40 || !(0.75..=1.33).contains(&(w as f64 / h as f64)) {
            self.reset_keyframe();
            return MinimapGateResult {
                valid: false,
                stationary: false,
                frame_diff: 255.0,
                alpha_diff: 255.0,
                center_diff: 255.0,
                inner_std: 0.0,
                border_coherence: 0.0,
                hdr_clipped: false,
                reason: Some("invalid_dimensions".to_owned()),
            };
        }

        let g_slice = gray64.as_slice().expect("contiguous gray64");
        let hdr_rep = detect_hdr_clipping(FrameView {
            width: Self::SZ as u32,
            height: Self::SZ as u32,
            stride: Self::SZ,
            format: PixelFormat::Gray8,
            data: g_slice,
        });

        let full_std = std_f64_u8(g_slice);
        let inner_vals: Vec<u8> = self.disk_indices.iter().map(|&i| g_slice[i]).collect();
        let inner_std = std_f64_u8(&inner_vals);
        let inner_mean = mean_f64_u8(&inner_vals);

        if full_std < self.min_std
            || inner_std < self.min_std
            || inner_mean < 6.0
            || inner_mean > 249.0
        {
            self.reset_keyframe();
            return MinimapGateResult {
                valid: false,
                stationary: false,
                frame_diff: 255.0,
                alpha_diff: 255.0,
                center_diff: 255.0,
                inner_std,
                border_coherence: 0.0,
                hdr_clipped: hdr_rep.clipped,
                reason: Some("blank_or_flash_frame".to_owned()),
            };
        }

        let coherence = self.circular_border_coherence(g_slice, inner_std);
        if self.require_circular_border && coherence < self.border_coherence {
            self.reset_keyframe();
            return MinimapGateResult {
                valid: false,
                stationary: false,
                frame_diff: 255.0,
                alpha_diff: 255.0,
                center_diff: 255.0,
                inner_std,
                border_coherence: coherence,
                hdr_clipped: hdr_rep.clipped,
                reason: Some("no_circular_minimap_border".to_owned()),
            };
        }

        let curr_annulus: Vec<u8> = self.annulus_indices.iter().map(|&i| g_slice[i]).collect();
        let frame_diff = match &self.keyframe_annulus {
            Some(kf) if kf.len() == curr_annulus.len() => mean_abs_diff_u8(&curr_annulus, kf),
            _ => 255.0,
        };
        self.last_annulus = Some(curr_annulus.clone());

        let curr_center: Vec<u8> = self.center_indices.iter().map(|&i| g_slice[i]).collect();
        let center_diff = match &self.keyframe_center {
            Some(kc) if kc.len() == curr_center.len() => mean_abs_diff_u8(&curr_center, kc),
            _ => 255.0,
        };

        let curr_alpha: Option<Vec<u8>> = alpha64.as_ref().map(|a| {
            let a_slice = a.as_slice().expect("contiguous alpha64");
            self.annulus_indices.iter().map(|&i| a_slice[i]).collect()
        });
        let alpha_diff = match (&curr_alpha, &self.keyframe_alpha) {
            (Some(ca), Some(ka)) if ca.len() == ka.len() => mean_abs_diff_u8(ca, ka),
            (Some(_), None) | (None, Some(_)) => 255.0,
            _ => 0.0,
        };

        let stationary = frame_diff < self.stationary_diff;
        if update_keyframe && !stationary {
            self.keyframe_annulus = Some(curr_annulus);
            self.keyframe_center = Some(curr_center);
            self.keyframe_alpha = curr_alpha;
        }

        MinimapGateResult {
            valid: true,
            stationary,
            frame_diff,
            alpha_diff,
            center_diff,
            inner_std,
            border_coherence: coherence,
            hdr_clipped: hdr_rep.clipped,
            reason: None,
        }
    }

    fn reject_empty(&mut self, reason: &str) -> MinimapGateResult {
        self.reset_keyframe();
        MinimapGateResult {
            valid: false,
            stationary: false,
            frame_diff: 255.0,
            alpha_diff: 255.0,
            center_diff: 255.0,
            inner_std: 0.0,
            border_coherence: 0.0,
            hdr_clipped: false,
            reason: Some(reason.to_owned()),
        }
    }

    pub fn check(&mut self, frame: Option<FrameView<'_>>, update_keyframe: bool) -> MinimapGateResult {
        let Some(view) = frame else {
            return self.reject_empty("empty_frame");
        };
        let (h, w) = (view.height as usize, view.width as usize);
        if h == 0 || w == 0 {
            return self.reject_empty("empty_frame");
        }
        if h.min(w) < 40 || !(0.75..=1.33).contains(&(w as f64 / h as f64)) {
            return self.reject_empty("invalid_dimensions");
        }
        let Some((g64, a64)) = Self::downscale_64_frame(view) else {
            return self.reject_empty("invalid_channels");
        };
        self.check_downscaled(h, w, g64, a64, update_keyframe)
    }

    pub fn check_array3(
        &mut self,
        minimap_img: Option<ArrayView3<'_, u8>>,
        update_keyframe: bool,
    ) -> MinimapGateResult {
        let Some(img) = minimap_img else {
            return self.reject_empty("empty_frame");
        };
        let (h, w, _) = img.dim();
        if img.is_empty() {
            return self.reject_empty("empty_frame");
        }
        if h.min(w) < 40 || !(0.75..=1.33).contains(&(w as f64 / h as f64)) {
            return self.reject_empty("invalid_dimensions");
        }
        let Some((g64, a64)) = Self::downscale_64_array3(img) else {
            return self.reject_empty("invalid_channels");
        };
        self.check_downscaled(h, w, g64, a64, update_keyframe)
    }

    pub fn check_gray(
        &mut self,
        minimap_gray: Option<ArrayView2<'_, u8>>,
        update_keyframe: bool,
    ) -> MinimapGateResult {
        let Some(img) = minimap_gray else {
            return self.reject_empty("empty_frame");
        };
        let (h, w) = img.dim();
        if img.is_empty() {
            return self.reject_empty("empty_frame");
        }
        if h.min(w) < 40 || !(0.75..=1.33).contains(&(w as f64 / h as f64)) {
            return self.reject_empty("invalid_dimensions");
        }
        let Some((g64, a64)) = Self::downscale_64_gray(img) else {
            return self.reject_empty("invalid_channels");
        };
        self.check_downscaled(h, w, g64, a64, update_keyframe)
    }

    pub fn commit_keyframe_array3(&mut self, img: ArrayView3<'_, u8>, cam_deg: Option<f64>) {
        let Some((g64, a64)) = Self::downscale_64_array3(img) else {
            self.reset_keyframe();
            return;
        };
        self.commit_downscaled(g64, a64, cam_deg);
    }

    pub fn commit_keyframe_gray(&mut self, img: ArrayView2<'_, u8>, cam_deg: Option<f64>) {
        let Some((g64, a64)) = Self::downscale_64_gray(img) else {
            self.reset_keyframe();
            return;
        };
        self.commit_downscaled(g64, a64, cam_deg);
    }

    fn commit_downscaled(
        &mut self,
        g64: Array2<u8>,
        a64: Option<Array2<u8>>,
        cam_deg: Option<f64>,
    ) {
        let gs = g64.as_slice().expect("contiguous");
        self.keyframe_annulus = Some(self.annulus_indices.iter().map(|&i| gs[i]).collect());
        self.keyframe_center = Some(self.center_indices.iter().map(|&i| gs[i]).collect());
        self.keyframe_alpha = a64.map(|a| {
            let as_ = a.as_slice().expect("contiguous");
            self.annulus_indices.iter().map(|&i| as_[i]).collect()
        });
        self.keyframe_cam_deg = cam_deg.map(wrap_angle_360);
    }

    pub fn commit_heading_keyframe_array3(&mut self, img: ArrayView3<'_, u8>) {
        let Some((g64, a64)) = Self::downscale_64_array3(img) else {
            return;
        };
        let gs = g64.as_slice().expect("contiguous");
        self.keyframe_center = Some(self.center_indices.iter().map(|&i| gs[i]).collect());
        if let Some(a) = a64 {
            let as_ = a.as_slice().expect("contiguous");
            self.keyframe_alpha = Some(self.annulus_indices.iter().map(|&i| as_[i]).collect());
        }
    }

    pub fn commit_heading_keyframe_gray(&mut self, img: ArrayView2<'_, u8>) {
        let Some((g64, _)) = Self::downscale_64_gray(img) else {
            return;
        };
        let gs = g64.as_slice().expect("contiguous");
        self.keyframe_center = Some(self.center_indices.iter().map(|&i| gs[i]).collect());
    }

    pub fn is_terrain_stationary_excluding_cones(
        &self,
        curr_cam_deg: Option<f64>,
        half_fov_deg: f64,
        diff_thresh: Option<f64>,
    ) -> bool {
        let (Some(kf), Some(last)) = (&self.keyframe_annulus, &self.last_annulus) else {
            return false;
        };
        if kf.len() != last.len() || kf.len() != self.annulus_angles_deg.len() {
            return false;
        }
        let n_ann = self.annulus_angles_deg.len();
        let mut sum_diff = 0u64;
        let mut keep_cnt = 0usize;
        for i in 0..n_ann {
            let ang_px = self.annulus_angles_deg[i] as f64;
            let mut keep = true;
            if let Some(c) = curr_cam_deg
                && shortest_angle_delta(c, ang_px).abs() <= half_fov_deg
            {
                keep = false;
            }
            if keep
                && let Some(k) = self.keyframe_cam_deg
                && shortest_angle_delta(k, ang_px).abs() <= half_fov_deg
            {
                keep = false;
            }
            if keep {
                sum_diff += last[i].abs_diff(kf[i]) as u64;
                keep_cnt += 1;
            }
        }
        let min_keep = 64usize.max((0.22 * n_ann as f64) as usize);
        if keep_cnt < min_keep {
            return false;
        }
        let mean_diff = sum_diff as f64 / keep_cnt as f64;
        let lim = diff_thresh.unwrap_or_else(|| 1.6f64.max(self.stationary_diff * 1.4));
        mean_diff < lim
    }
}

pub struct TrackerFSM {
    pub config: MinimapTrackerConfig,
    pub state: TrackerState,
    pub map_id: Option<i32>,
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub is_underground: bool,
    pub floor_id: Option<i32>,
    pub floor_name: String,
    pub confidence: f64,
    pub last_timestamp: Option<f64>,
    pub last_accepted_timestamp: Option<f64>,
    pub last_absolute_fix_timestamp: Option<f64>,

    candidate_x: Option<f64>,
    candidate_y: Option<f64>,
    candidate_map_id: Option<i32>,
    candidate_is_underground: bool,
    candidate_floor_id: Option<i32>,
    candidate_floor_name: String,
    candidate_confidence: f64,
    candidate_match_method: String,
    candidate_hits: usize,
}

impl TrackerFSM {
    pub fn new(config: MinimapTrackerConfig) -> CvResult<Self> {
        config.validate()?;
        Ok(Self {
            config,
            state: TrackerState::Lost,
            map_id: None,
            x: None,
            y: None,
            is_underground: false,
            floor_id: None,
            floor_name: "G".to_owned(),
            confidence: 0.0,
            last_timestamp: None,
            last_accepted_timestamp: None,
            last_absolute_fix_timestamp: None,
            candidate_x: None,
            candidate_y: None,
            candidate_map_id: None,
            candidate_is_underground: false,
            candidate_floor_id: None,
            candidate_floor_name: "G".to_owned(),
            candidate_confidence: 0.0,
            candidate_match_method: "none".to_owned(),
            candidate_hits: 0,
        })
    }

    fn clear_candidate(&mut self) {
        self.candidate_x = None;
        self.candidate_y = None;
        self.candidate_map_id = None;
        self.candidate_is_underground = false;
        self.candidate_floor_id = None;
        self.candidate_floor_name = "G".to_owned();
        self.candidate_confidence = 0.0;
        self.candidate_match_method = "none".to_owned();
        self.candidate_hits = 0;
    }

    fn valid(&self, m: &PoseMeasurement) -> bool {
        if !(m.found && m.map_id.is_some() && m.world_x.is_some() && m.world_y.is_some()) {
            return false;
        }
        if m.match_method == "motion" {
            m.confidence >= 0.30
        } else {
            m.confidence >= self.config.min_confidence
        }
    }

    fn set_candidate(&mut self, m: &PoseMeasurement) {
        self.candidate_x = m.world_x;
        self.candidate_y = m.world_y;
        self.candidate_map_id = m.map_id;
        self.candidate_is_underground = m.is_underground;
        self.candidate_floor_id = m.floor_id;
        self.candidate_floor_name = if m.floor_name.is_empty() {
            if m.is_underground { "B1" } else { "G" }.to_owned()
        } else {
            m.floor_name.clone()
        };
        self.candidate_confidence = m.confidence;
        self.candidate_match_method = m.match_method.clone();
        self.candidate_hits = 1;
    }

    fn update_candidate(&mut self, m: &PoseMeasurement) -> bool {
        let (Some(x), Some(y), Some(mid)) = (m.world_x, m.world_y, m.map_id) else {
            return false;
        };
        let (Some(cx), Some(cy)) = (self.candidate_x, self.candidate_y) else {
            self.set_candidate(m);
            return false;
        };
        if Some(mid) != self.candidate_map_id
            || m.is_underground != self.candidate_is_underground
            || m.floor_id != self.candidate_floor_id
            || (x - cx).hypot(y - cy) > self.config.candidate_radius_world
        {
            self.set_candidate(m);
            return false;
        }
        let count = self.candidate_hits + 1;
        self.candidate_x = Some(cx + (x - cx) / (count as f64));
        self.candidate_y = Some(cy + (y - cy) / (count as f64));
        self.candidate_confidence = m.confidence;
        self.candidate_match_method = m.match_method.clone();
        self.candidate_hits = count;
        true
    }

    fn accept_candidate(&mut self, timestamp: f64) {
        self.x = self.candidate_x;
        self.y = self.candidate_y;
        self.map_id = self.candidate_map_id;
        self.is_underground = self.candidate_is_underground;
        self.floor_id = self.candidate_floor_id;
        self.floor_name = self.candidate_floor_name.clone();
        self.confidence = self.candidate_confidence;
        self.last_accepted_timestamp = Some(timestamp);
        if self.candidate_match_method != "motion" {
            self.last_absolute_fix_timestamp = Some(timestamp);
        }
        self.clear_candidate();
        self.state = TrackerState::Tracking;
    }

    fn check_monotonic(&self, ts: f64) -> CvResult<()> {
        if let Some(prev) = self.last_timestamp
            && ts < prev
        {
            return Err(CvError::InvalidArgument(
                "Tracker timestamps must be monotonic".into(),
            ));
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_anchor(
        &mut self,
        map_id: i32,
        world_x: f64,
        world_y: f64,
        timestamp: f64,
        is_underground: bool,
        floor_id: Option<i32>,
        floor_name: &str,
    ) -> CvResult<()> {
        self.check_monotonic(timestamp)?;
        self.last_timestamp = Some(timestamp);
        self.last_accepted_timestamp = Some(timestamp);
        self.last_absolute_fix_timestamp = Some(timestamp);
        self.map_id = Some(map_id);
        self.x = Some(world_x);
        self.y = Some(world_y);
        self.is_underground = is_underground;
        self.floor_id = floor_id;
        self.floor_name = if floor_name.is_empty() {
            if is_underground { "B1" } else { "G" }.to_owned()
        } else {
            floor_name.to_owned()
        };
        self.confidence = 1.0;
        self.clear_candidate();
        self.state = TrackerState::Tracking;
        Ok(())
    }

    pub fn set_floor(&mut self, is_underground: bool, floor_id: Option<i32>, floor_name: &str) {
        self.is_underground = is_underground;
        self.floor_id = floor_id;
        self.floor_name = if floor_name.is_empty() {
            if is_underground { "B1" } else { "G" }.to_owned()
        } else {
            floor_name.to_owned()
        };
    }

    pub fn force_lost(&mut self, timestamp: f64) -> CvResult<()> {
        self.check_monotonic(timestamp)?;
        self.last_timestamp = Some(timestamp);
        self.last_accepted_timestamp = None;
        self.state = TrackerState::Lost;
        self.x = None;
        self.y = None;
        self.confidence = 0.0;
        self.clear_candidate();
        Ok(())
    }

    pub fn touch_stationary(&mut self, timestamp: f64) -> CvResult<()> {
        self.check_monotonic(timestamp)?;
        self.last_timestamp = Some(timestamp);
        if self.state == TrackerState::Tracking && self.x.is_some() && self.y.is_some() {
            self.last_accepted_timestamp = Some(timestamp);
        }
        Ok(())
    }

    pub fn pause(&mut self, timestamp: f64) -> CvResult<()> {
        self.check_monotonic(timestamp)?;
        if let (Some(lt), Some(lat)) = (self.last_timestamp, self.last_accepted_timestamp) {
            self.last_accepted_timestamp = Some(lat + (timestamp - lt));
        }
        self.last_timestamp = Some(timestamp);
        Ok(())
    }

    pub fn update(
        &mut self,
        m: &PoseMeasurement,
        timestamp: f64,
    ) -> CvResult<(bool, bool, Option<String>)> {
        self.check_monotonic(timestamp)?;
        let delta = self.last_timestamp.map_or(0.0, |lt| timestamp - lt);
        self.last_timestamp = Some(timestamp);

        if !self.valid(m) {
            let expired = self
                .last_accepted_timestamp
                .is_none_or(|lat| (timestamp - lat) > self.config.lost_timeout_seconds);
            if expired {
                self.state = TrackerState::Lost;
                self.x = None;
                self.y = None;
                self.confidence = 0.0;
                self.clear_candidate();
            } else if matches!(self.state, TrackerState::Acquiring | TrackerState::Relocating) {
                self.state = TrackerState::Tracking;
            }
            let reason = m
                .reason
                .clone()
                .or_else(|| Some("localization_below_threshold".to_owned()));
            return Ok((false, !expired, reason));
        }

        let x = m.world_x.unwrap();
        let y = m.world_y.unwrap();
        let mid = m.map_id.unwrap();

        if self.state == TrackerState::Lost {
            self.set_candidate(m);
            if self.config.acquire_hits <= 1 {
                self.accept_candidate(timestamp);
                return Ok((true, false, None));
            }
            self.state = TrackerState::Acquiring;
            return Ok((false, false, Some("awaiting_confirmation".to_owned())));
        }

        if self.state == TrackerState::Acquiring {
            self.update_candidate(m);
            if self.candidate_hits >= self.config.acquire_hits {
                self.accept_candidate(timestamp);
                return Ok((true, false, None));
            }
            return Ok((false, false, Some("awaiting_confirmation".to_owned())));
        }

        let (Some(cur_x), Some(cur_y)) = (self.x, self.y) else {
            self.set_candidate(m);
            self.state = TrackerState::Acquiring;
            return Ok((false, false, Some("awaiting_confirmation".to_owned())));
        };

        let allowed_distance =
            self.config.max_speed_world_per_second * delta + self.config.jump_margin_world;
        let distance_from_track = (x - cur_x).hypot(y - cur_y);
        let same_layer = Some(mid) == self.map_id
            && m.is_underground == self.is_underground
            && m.floor_id == self.floor_id;

        if same_layer && distance_from_track <= allowed_distance {
            let alpha = self.config.smoothing_alpha;
            self.x = Some(cur_x + alpha * (x - cur_x));
            self.y = Some(cur_y + alpha * (y - cur_y));
            self.last_accepted_timestamp = Some(timestamp);
            self.clear_candidate();
            self.state = TrackerState::Tracking;
            self.confidence = m.confidence;
            if m.match_method != "motion" {
                self.last_absolute_fix_timestamp = Some(timestamp);
            }
            return Ok((true, false, None));
        }

        if self.state != TrackerState::Relocating {
            self.set_candidate(m);
            self.state = TrackerState::Relocating;
        } else {
            self.update_candidate(m);
        }

        if self.candidate_hits >= self.config.relocate_hits {
            self.accept_candidate(timestamp);
            return Ok((true, false, Some("relocation_confirmed".to_owned())));
        }
        Ok((false, true, Some("possible_relocation".to_owned())))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct RefCellKey {
    map_id: i32,
    is_underground: bool,
    floor_id: Option<i32>,
    zoom_tier: i32,
    cx: i64,
    cy: i64,
}

struct RefCellRecord {
    sobel: Array2<f32>,
    sobel_half: Option<Array2<f32>>,
    kp: Vec<KeyPoint>,
    des: Vec<[f32; 128]>,
    ox: f64,
    oy: f64,
    ppu: f64,
    dl_seq: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct UgOverlayKey {
    map_id: i32,
    group_id: i32,
    floor_id: i32,
}

struct CachedUgOverlay {
    gray: Array2<u8>,
    bounds: OverlayBounds,
}

pub struct HybridMinimapTracker {
    pub config: MinimapTrackerConfig,
    pub tile_cache: Arc<LruTerrainTileCache>,
    pub gate: MinimapGate,
    pub fsm: TrackerFSM,

    multimap_db: Option<Arc<MultimapDb>>,
    ug_provider: Option<Arc<dyn UndergroundOverlaySource>>,
    global_relocator: Option<Arc<TeyvatGlobalRelocator>>,
    global_locator: Option<GlobalLocatorFn>,

    camera_heading: Option<f64>,
    body_heading: Option<f64>,
    view_heading: Option<f64>,
    camera_conf: f64,
    body_conf: f64,
    body_smoother: CircularAngleSmoother,
    camera_smoother: CircularAngleSmoother,
    camera_fuser: CameraHeadingFuser,
    camera_method: FuserSource,
    cone_raw_heading: Option<f64>,

    minimap_mode: MinimapMode,
    map_rotation_deg: f64,

    zoom_regime: ZoomRegime,
    zoom_alt_streak: usize,
    confirmed_span_world: f64,
    span_confirmed: bool,
    last_zoom_tier: i32,

    floor_switch_streak: usize,
    floor_switch_candidate_id: Option<i32>,
    layer_probe_tick: usize,
    pending_layer_key: Option<(bool, Option<i32>, String)>,
    pending_layer_score: usize,

    pause_start_ts: Option<f64>,
    long_pause_pending: bool,
    just_resumed_from_pause: bool,
    resume_frames_left: usize,
    relocate_tier2_left: usize,
    consecutive_sift_misses: usize,
    sift_interframe_budget: usize,
    global_vote_bins: VoteBins,
    global_query_count: usize,
    last_sift_inliers: usize,

    paimon_cache_path: Option<PathBuf>,
    has_paimon_tpl: bool,
    paimon_tpl: Option<Array2<u8>>,
    paimon_mask: Option<Array2<u8>>,
    paimon_mask_indices: Vec<usize>,
    paimon_t_zm: Vec<f32>,
    paimon_t_norm: f32,
    paimon_tpl_half: Option<Array2<u8>>,
    paimon_mask_half: Option<Array2<u8>>,
    paimon_last_xy_1080: Option<(i32, i32)>,
    paimon_check_tick: usize,
    paimon_bootstrap_streak: usize,
    layout_cache: HashMap<(u32, u32, bool), UiLayout>,
    hud_frame_size: Option<(u32, u32, bool)>,
    cal_offset_samples: Vec<(i32, i32)>,
    cal_dx_px: i32,
    cal_dy_px: i32,
    cal_locked: bool,

    phase_size: usize,
    hann_window: Array2<f32>,
    prev_phase_gray: Option<Array2<f32>>,
    prev_mm_gray_for_phase: Option<Array2<u8>>,
    prev_cone_excl_for_phase: Option<f64>,
    motion_fallback_frames: usize,

    annulus_masks: HashMap<usize, Array2<u8>>,
    sift_mm: SiftExtractor,
    sift_ref: SiftExtractor,
    cell_cache: VecDeque<(RefCellKey, RefCellRecord)>,
    max_cells: usize,
    ug_overlay_cache: VecDeque<(UgOverlayKey, CachedUgOverlay)>,
    max_ug_overlays: usize,
    orb: OrbExtractor,

    matcher_calls: u64,
    stationary_skips: u64,
    last_match_ms: f64,
    last_hdr_clipped: bool,
    last_timestamp: f64,
    start_instant: Instant,
}

impl HybridMinimapTracker {
    pub fn new(
        tile_cache: impl Into<Arc<LruTerrainTileCache>>,
        config: MinimapTrackerConfig,
    ) -> CvResult<Self> {
        let fsm = TrackerFSM::new(config.clone())?;
        let gate = MinimapGate::new(
            config.gate_min_std,
            config.gate_stationary_diff,
            config.gate_border_coherence,
            true,
        );
        let phase_size = 96usize;
        let hann_window = create_hanning_window(phase_size, phase_size)?;
        let confirmed_span = config.open_world_span_world;

        Ok(Self {
            config,
            tile_cache: tile_cache.into(),
            gate,
            fsm,
            multimap_db: None,
            ug_provider: None,
            global_relocator: None,
            global_locator: None,
            camera_heading: None,
            body_heading: None,
            view_heading: None,
            camera_conf: 0.0,
            body_conf: 0.0,
            body_smoother: CircularAngleSmoother::new(0.75, 0.6)?,
            camera_smoother: CircularAngleSmoother::new(0.82, 0.5)?,
            camera_fuser: CameraHeadingFuser::default(),
            camera_method: FuserSource::None,
            cone_raw_heading: None,
            minimap_mode: MinimapMode::LockDirection,
            map_rotation_deg: 0.0,
            zoom_regime: ZoomRegime::OpenWorld,
            zoom_alt_streak: 0,
            confirmed_span_world: confirmed_span,
            span_confirmed: false,
            last_zoom_tier: -1,
            floor_switch_streak: 0,
            floor_switch_candidate_id: None,
            layer_probe_tick: 0,
            pending_layer_key: None,
            pending_layer_score: 0,
            pause_start_ts: None,
            long_pause_pending: false,
            just_resumed_from_pause: false,
            resume_frames_left: 0,
            relocate_tier2_left: 0,
            consecutive_sift_misses: 0,
            sift_interframe_budget: 0,
            global_vote_bins: VoteBins::new(),
            global_query_count: 0,
            last_sift_inliers: 0,
            paimon_cache_path: None,
            has_paimon_tpl: false,
            paimon_tpl: None,
            paimon_mask: None,
            paimon_mask_indices: Vec::new(),
            paimon_t_zm: Vec::new(),
            paimon_t_norm: 1.0,
            paimon_tpl_half: None,
            paimon_mask_half: None,
            paimon_last_xy_1080: None,
            paimon_check_tick: 0,
            paimon_bootstrap_streak: 0,
            layout_cache: HashMap::new(),
            hud_frame_size: None,
            cal_offset_samples: Vec::new(),
            cal_dx_px: 0,
            cal_dy_px: 0,
            cal_locked: false,
            phase_size,
            hann_window,
            prev_phase_gray: None,
            prev_mm_gray_for_phase: None,
            prev_cone_excl_for_phase: None,
            motion_fallback_frames: 0,
            annulus_masks: HashMap::new(),
            sift_mm: SiftExtractor::new(300, 0.012, 15.0),
            sift_ref: SiftExtractor::new(950, 0.011, 15.0),
            cell_cache: VecDeque::with_capacity(18),
            max_cells: 18,
            ug_overlay_cache: VecDeque::with_capacity(12),
            max_ug_overlays: 12,
            orb: OrbExtractor::new(OrbParams {
                nfeatures: 800,
                scale_factor: 1.15,
                nlevels: 6,
                edge_threshold: 11,
                patch_size: 15,
                fast_threshold: 7,
            }),
            matcher_calls: 0,
            stationary_skips: 0,
            last_match_ms: 0.0,
            last_hdr_clipped: false,
            last_timestamp: 0.0,
            start_instant: Instant::now(),
        })
    }

    pub fn with_gate(mut self, gate: MinimapGate) -> Self {
        self.gate = gate;
        self
    }

    pub fn with_multimap_db(mut self, db: Arc<MultimapDb>) -> Self {
        self.multimap_db = Some(db);
        self
    }

    pub fn with_ug_provider(mut self, ug: Arc<dyn UndergroundOverlaySource>) -> Self {
        self.ug_provider = Some(ug);
        self
    }

    pub fn with_global_relocator(mut self, reloc: Arc<TeyvatGlobalRelocator>) -> Self {
        self.global_relocator = Some(reloc);
        self
    }

    pub fn with_global_locator(mut self, loc: GlobalLocatorFn) -> Self {
        self.global_locator = Some(loc);
        self
    }

    pub fn with_paimon_cache_path(mut self, path: impl Into<PathBuf>) -> Self {
        self.paimon_cache_path = Some(path.into());
        if !self.has_paimon_tpl {
            self.try_load_cached_paimon();
        }
        self
    }

    pub fn with_paimon_template(
        mut self,
        tpl_bgr_or_gray: ArrayView3<'_, u8>,
        mask: ArrayView2<'_, u8>,
    ) -> Self {
        let (_, _, c) = tpl_bgr_or_gray.dim();
        let gray = if c == 1 {
            Some(tpl_bgr_or_gray.slice(s![.., .., 0]).to_owned())
        } else if c >= 3 {
            bgr_to_gray(tpl_bgr_or_gray).ok()
        } else {
            None
        };
        if let Some(g) = gray {
            self.init_paimon_matcher(g.view(), mask);
        }
        self
    }

    pub fn with_paimon_template_gray(
        mut self,
        tpl_gray: ArrayView2<'_, u8>,
        mask: ArrayView2<'_, u8>,
    ) -> Self {
        self.init_paimon_matcher(tpl_gray, mask);
        self
    }

    #[inline]
    pub fn state(&self) -> TrackerState {
        self.fsm.state
    }

    #[inline]
    pub fn zoom_regime(&self) -> ZoomRegime {
        self.zoom_regime
    }

    #[inline]
    pub fn minimap_mode(&self) -> MinimapMode {
        self.minimap_mode
    }

    #[inline]
    pub fn calibrated_offset_px(&self) -> (i32, i32) {
        (self.cal_dx_px, self.cal_dy_px)
    }

    #[inline]
    pub fn has_paimon_tpl(&self) -> bool {
        self.has_paimon_tpl
    }

    #[inline]
    pub fn camera_method(&self) -> FuserSource {
        self.camera_method
    }

    #[inline]
    pub fn cone_raw_heading(&self) -> Option<f64> {
        self.cone_raw_heading
    }

    #[inline]
    pub fn matcher_calls(&self) -> u64 {
        self.matcher_calls
    }

    #[inline]
    pub fn stationary_skips(&self) -> u64 {
        self.stationary_skips
    }

    #[inline]
    pub fn last_match_ms(&self) -> f64 {
        self.last_match_ms
    }

    fn init_paimon_matcher(&mut self, tpl_gray: ArrayView2<'_, u8>, tpl_mask: ArrayView2<'_, u8>) {
        let (ht, wt) = tpl_gray.dim();
        if ht == 0 || wt == 0 || tpl_mask.dim() != (ht, wt) {
            self.has_paimon_tpl = false;
            return;
        }
        let g = tpl_gray.as_standard_layout().into_owned();
        let m = tpl_mask.mapv(|v| if v > 0 { 255u8 } else { 0u8 });

        let g_slice = g.as_slice().expect("contiguous");
        let m_slice = m.as_slice().expect("contiguous");
        let mut indices = Vec::new();
        let mut t_vals = Vec::new();
        for (idx, (&mv, &gv)) in m_slice.iter().zip(g_slice.iter()).enumerate() {
            if mv > 0 {
                indices.push(idx);
                t_vals.push(gv as f32);
            }
        }
        if indices.is_empty() {
            self.has_paimon_tpl = false;
            return;
        }
        let t_mean = t_vals.iter().map(|&v| v as f64).sum::<f64>() / (t_vals.len() as f64);
        let mut t_zm = Vec::with_capacity(t_vals.len());
        let mut sq_sum = 0.0f64;
        for v in t_vals {
            let d = v - (t_mean as f32);
            sq_sum += (d as f64) * (d as f64);
            t_zm.push(d);
        }
        let t_norm = (sq_sum.sqrt() as f32) + 1e-6;

        let hw_h = (wt / 2).max(12);
        let hh_h = (ht / 2).max(12);
        let Ok(tpl_half) = resize_u8(g.view(), hw_h, hh_h, Interpolation::Area) else {
            self.has_paimon_tpl = false;
            return;
        };
        let Ok(mask_half_raw) = resize_u8(m.view(), hw_h, hh_h, Interpolation::Nearest) else {
            self.has_paimon_tpl = false;
            return;
        };
        let mask_half = mask_half_raw.mapv(|v| if v > 0 { 255u8 } else { 0u8 });

        self.paimon_tpl = Some(g);
        self.paimon_mask = Some(m);
        self.paimon_mask_indices = indices;
        self.paimon_t_zm = t_zm;
        self.paimon_t_norm = t_norm;
        self.paimon_tpl_half = Some(tpl_half);
        self.paimon_mask_half = Some(mask_half);
        self.has_paimon_tpl = true;
    }

    fn try_load_cached_paimon(&mut self) {
        let Some(p) = self.paimon_cache_path.as_ref() else {
            return;
        };
        if !p.is_file() {
            return;
        }
        let Ok(archive) = NpzArchive::read_file(p) else {
            return;
        };
        let (Some(tpl_arr), Some(mask_arr)) = (archive.get("tpl_gray"), archive.get("mask")) else {
            return;
        };
        if tpl_arr.shape.len() != 2 || mask_arr.shape != tpl_arr.shape {
            return;
        }
        let (NpyData::U8(tpl_bytes), NpyData::U8(mask_bytes)) = (&tpl_arr.data, &mask_arr.data)
        else {
            return;
        };
        let (h, w) = (tpl_arr.shape[0], tpl_arr.shape[1]);
        let (Ok(tpl), Ok(mask)) = (
            Array2::from_shape_vec((h, w), tpl_bytes.clone()),
            Array2::from_shape_vec((h, w), mask_bytes.clone()),
        ) else {
            return;
        };
        self.init_paimon_matcher(tpl.view(), mask.view());
    }

    fn try_bootstrap_paimon(&mut self, hud_tl_gray: ArrayView2<'_, u8>, lay: &UiLayout) {
        if self.has_paimon_tpl || self.paimon_cache_path.is_none() {
            return;
        }
        let (hh, hw) = hud_tl_gray.dim();
        let px = lay.paimon_rect.x.max(0) as usize;
        let py = lay.paimon_rect.y.max(0) as usize;
        let pw = lay.paimon_rect.width.max(0) as usize;
        let ph = lay.paimon_rect.height.max(0) as usize;
        let x1 = hw.min(px + pw);
        let y1 = hh.min(py + ph);
        if x1.saturating_sub(px) < 16 || y1.saturating_sub(py) < 16 {
            return;
        }
        let crop = hud_tl_gray.slice(s![py..y1, px..x1]);
        let crop_owned = crop.as_standard_layout().into_owned();
        if std_f64_u8(crop_owned.as_slice().unwrap()) < 12.0 {
            return;
        }
        let (ch, cw) = crop_owned.dim();
        let tpl_gray = if (ch, cw) == (77, 68) {
            crop_owned
        } else {
            let interp = if cw > 68 {
                Interpolation::Area
            } else {
                Interpolation::Linear
            };
            let Ok(r) = resize_u8(crop_owned.view(), 68, 77, interp) else {
                return;
            };
            r
        };
        let mut tpl_mask = Array2::<u8>::zeros((77, 68));
        for y in 0..77 {
            let dy = (y as f64 - 38.0) / 35.0;
            for x in 0..68 {
                let dx = (x as f64 - 33.5) / 30.0;
                if dx * dx + dy * dy <= 1.0 {
                    tpl_mask[[y, x]] = 255;
                }
            }
        }
        if let Some(cache_path) = &self.paimon_cache_path {
            let mut archive = NpzArchive::new();
            if let (Ok(a_tpl), Ok(a_mask)) = (
                NpyArray::from_u8(vec![77, 68], tpl_gray.iter().copied().collect()),
                NpyArray::from_u8(vec![77, 68], tpl_mask.iter().copied().collect()),
            ) {
                archive.insert("tpl_gray", a_tpl);
                archive.insert("mask", a_mask);
                let _ = archive.write_file_atomic(cache_path);
            }
        }
        self.init_paimon_matcher(tpl_gray.view(), tpl_mask.view());
    }

    fn check_paimon(&mut self, paimon_maybe_gray: Option<ArrayView2<'_, u8>>, ui_scale: f64) -> f64 {
        if !self.has_paimon_tpl {
            return 1.0;
        }
        let (
            Some(paimon_tpl),
            Some(paimon_mask),
            Some(paimon_tpl_half),
            Some(paimon_mask_half),
            Some(pm),
        ) = (
            self.paimon_tpl.as_ref(),
            self.paimon_mask.as_ref(),
            self.paimon_tpl_half.as_ref(),
            self.paimon_mask_half.as_ref(),
            paimon_maybe_gray,
        )
        else {
            return 1.0;
        };
        let (mh, mw) = pm.dim();
        if mh == 0 || mw == 0 {
            return 1.0;
        }
        let (th, tw) = paimon_tpl.dim();
        let sc = ui_scale.max(1e-6);
        let inv_s = 1.0 / sc;
        let rw = tw.max((mw as f64 * inv_s).round_ties_even() as usize);
        let rh = th.max((mh as f64 * inv_s).round_ties_even() as usize);

        if let Some((lx, ly)) = self.paimon_last_xy_1080
            && lx >= 0
            && ly >= 0
            && (lx as usize + tw) <= rw
            && (ly as usize + th) <= rh
        {
            let px0 = ((lx as f64 * sc).round_ties_even() as isize).clamp(0, mw as isize - 1)
                as usize;
            let py0 = ((ly as f64 * sc).round_ties_even() as isize).clamp(0, mh as isize - 1)
                as usize;
            let px1 = mw.min((px0 + 1).max(((lx + tw as i32) as f64 * sc).round_ties_even() as usize));
            let py1 = mh.min((py0 + 1).max(((ly + th as i32) as f64 * sc).round_ties_even() as usize));
            let sub = pm.slice(s![py0..py1, px0..px1]);
            let g_sub = if sub.dim() == (th, tw) {
                sub.as_standard_layout().into_owned()
            } else {
                let interp = if sub.dim().1 > tw {
                    Interpolation::Area
                } else {
                    Interpolation::Linear
                };
                match resize_u8(sub, tw, th, interp) {
                    Ok(r) => r,
                    Err(_) => return 1.0,
                }
            };
            let gs = g_sub.as_slice().expect("contiguous");
            let mut sum_g = 0.0f64;
            for &idx in &self.paimon_mask_indices {
                sum_g += gs[idx] as f64;
            }
            let mean_g = (sum_g / (self.paimon_mask_indices.len() as f64)) as f32;
            let mut dot = 0.0f64;
            let mut sq_g = 0.0f64;
            for (&idx, &tv) in self.paimon_mask_indices.iter().zip(self.paimon_t_zm.iter()) {
                let dv = (gs[idx] as f32 - mean_g) as f64;
                dot += dv * (tv as f64);
                sq_g += dv * dv;
            }
            let g_norm = sq_g.sqrt() + 1e-6;
            let ncc = dot / (g_norm * self.paimon_t_norm as f64);
            if ncc.is_finite() && ncc >= self.config.paimon_thresh {
                self.paimon_check_tick += 1;
                return ncc;
            }
        }

        self.paimon_check_tick += 1;
        let (ht_h, wt_h) = paimon_tpl_half.dim();
        let rw_h = wt_h.max(rw / 2);
        let rh_h = ht_h.max(rh / 2);
        let Ok(gray_h) = resize_u8(pm, rw_h, rh_h, Interpolation::Area) else {
            return 0.0;
        };
        let Ok(res_h) = match_template_masked_u8(
            gray_h.view(),
            paimon_tpl_half.view(),
            paimon_mask_half.view(),
            TemplateMatchMode::CcoeffNormed,
        ) else {
            return 0.0;
        };
        let Ok((_, mv_h_raw, _, ml_h)) = min_max_loc_f32(res_h.view()) else {
            return 0.0;
        };
        let mv_h = mv_h_raw as f64;
        if mv_h < self.config.paimon_thresh - 0.12 {
            return mv_h.max(0.0);
        }

        let cx_1080 = (ml_h.0 * 2) as isize;
        let cy_1080 = (ml_h.1 * 2) as isize;
        let x0 = (cx_1080 - 4).max(0) as usize;
        let y0 = (cy_1080 - 4).max(0) as usize;
        let x1 = rw.min(x0 + tw + 8);
        let y1 = rh.min(y0 + th + 8);
        let tw_1080 = x1.saturating_sub(x0);
        let th_1080 = y1.saturating_sub(y0);
        if tw_1080 >= tw && th_1080 >= th {
            let px0 = ((x0 as f64 * sc).round_ties_even() as isize).clamp(0, mw as isize - 1)
                as usize;
            let py0 = ((y0 as f64 * sc).round_ties_even() as isize).clamp(0, mh as isize - 1)
                as usize;
            let px1 = mw.min((px0 + 1).max((x1 as f64 * sc).round_ties_even() as usize));
            let py1 = mh.min((py0 + 1).max((y1 as f64 * sc).round_ties_even() as usize));
            let sub = pm.slice(s![py0..py1, px0..px1]);
            let gray_sub = if sub.dim() == (th_1080, tw_1080) {
                sub.as_standard_layout().into_owned()
            } else {
                let interp = if sub.dim().1 > tw_1080 {
                    Interpolation::Area
                } else {
                    Interpolation::Linear
                };
                match resize_u8(sub, tw_1080, th_1080, interp) {
                    Ok(r) => r,
                    Err(_) => return mv_h.max(0.0),
                }
            };
            if let Ok(res) = match_template_masked_u8(
                gray_sub.view(),
                paimon_tpl.view(),
                paimon_mask.view(),
                TemplateMatchMode::CcoeffNormed,
            ) && let Ok((_, mv_raw, _, ml)) = min_max_loc_f32(res.view())
            {
                let mv = mv_raw as f64;
                if mv >= self.config.paimon_thresh {
                    self.paimon_last_xy_1080 = Some(((x0 + ml.0) as i32, (y0 + ml.1) as i32));
                }
                return mv;
            }
        }
        mv_h.max(0.0)
    }

    fn get_layout(&mut self, frame_w: u32, frame_h: u32, gamepad_mode: bool) -> CvResult<UiLayout> {
        let key = (frame_w, frame_h, gamepad_mode);
        if let Some(&lay) = self.layout_cache.get(&key) {
            return Ok(lay);
        }
        let lay = compute_ui_layout(frame_w, frame_h, gamepad_mode)?;
        self.layout_cache.insert(key, lay);
        Ok(lay)
    }

    fn resolve_timestamp(&self, now: Option<f64>) -> f64 {
        if let Some(t) = now {
            return t;
        }
        let ts = self.start_instant.elapsed().as_secs_f64();
        if let Some(prev) = self.fsm.last_timestamp
            && ts < prev
        {
            return prev;
        }
        ts
    }

    fn get_annulus_mask(&mut self, size: usize) -> &Array2<u8> {
        if !self.annulus_masks.contains_key(&size) {
            let mut mask = Array2::<u8>::zeros((size, size));
            let c = (size.saturating_sub(1)) as f64 * 0.5;
            let r_max = size as f64 * 0.5;
            let inner_r = self.config.annulus_inner_ratio * r_max;
            let outer_r = self.config.annulus_outer_ratio * r_max;
            for y in 0..size {
                let dy = y as f64 - c;
                for x in 0..size {
                    let dx = x as f64 - c;
                    let rr = dx.hypot(dy);
                    if rr > inner_r && rr <= outer_r {
                        mask[[y, x]] = 255;
                    }
                }
            }
            self.annulus_masks.insert(size, mask);
        }
        self.annulus_masks.get(&size).unwrap()
    }

    fn span_for_regime(&self, regime: ZoomRegime) -> f64 {
        match regime {
            ZoomRegime::City => self.config.city_span_world,
            ZoomRegime::OpenWorld => self.config.open_world_span_world,
        }
    }

    fn patch_params_for_regime(&self, regime: ZoomRegime) -> (f64, f64, f64) {
        match regime {
            ZoomRegime::City => (
                self.config.city_span_world,
                self.config.search_radius_city,
                self.config.city_patch_px_per_unit,
            ),
            ZoomRegime::OpenWorld => (
                self.config.open_world_span_world,
                self.config.search_radius_open_world,
                self.config.open_world_patch_px_per_unit,
            ),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn build_pose(
        &self,
        timestamp: f64,
        stationary: bool,
        match_method: &str,
        reason: Option<String>,
        should_broadcast_web: bool,
        paimon_score: f64,
        sift_inliers: usize,
    ) -> MinimapPose {
        MinimapPose {
            state: self.fsm.state,
            map_id: self.fsm.map_id,
            world_x: self.fsm.x,
            world_y: self.fsm.y,
            lat: self.fsm.y,
            lng: self.fsm.x,
            camera_heading: self.camera_heading,
            body_heading: self.body_heading,
            view_heading: self.view_heading,
            minimap_mode: self.minimap_mode,
            zoom_regime: self.zoom_regime,
            is_underground: self.fsm.is_underground,
            floor_id: self.fsm.floor_id,
            confidence: self.fsm.confidence,
            timestamp,
            hdr_clipped: self.last_hdr_clipped,
            floor_name: self.fsm.floor_name.clone(),
            stationary,
            match_method: match_method.to_owned(),
            map_rotation_deg: self.map_rotation_deg,
            reason,
            should_broadcast_web,
            span_world: self.confirmed_span_world,
            paimon_score,
            sift_inliers,
            zoom_tier: self.last_zoom_tier,
            cal_dx_px: self.cal_dx_px,
            cal_dy_px: self.cal_dy_px,
        }
    }

    pub fn minimap_capture_rect(
        &mut self,
        frame_w: u32,
        frame_h: u32,
        gamepad_mode: bool,
    ) -> CvResult<RectI32> {
        let lay = self.get_layout(frame_w, frame_h, gamepad_mode)?;
        Ok(RectI32 {
            x: (lay.minimap_rect.x + self.cal_dx_px).max(0),
            y: (lay.minimap_rect.y + self.cal_dy_px).max(0),
            width: lay.minimap_rect.width,
            height: lay.minimap_rect.height,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_map_m_anchor(
        &mut self,
        map_id: i32,
        world_x: f64,
        world_y: f64,
        is_underground: bool,
        floor_id: Option<i32>,
        floor_name: &str,
        timestamp: Option<f64>,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(timestamp);
        self.last_timestamp = ts;
        self.fsm.set_anchor(
            map_id,
            world_x,
            world_y,
            ts,
            is_underground,
            floor_id,
            floor_name,
        )?;
        self.motion_fallback_frames = 0;
        self.prev_phase_gray = None;
        self.prev_mm_gray_for_phase = None;
        self.prev_cone_excl_for_phase = None;
        self.zoom_regime = ZoomRegime::OpenWorld;
        self.zoom_alt_streak = 0;
        self.confirmed_span_world = self.config.open_world_span_world;
        self.span_confirmed = false;
        self.last_zoom_tier = -1;
        self.floor_switch_streak = 0;
        self.floor_switch_candidate_id = None;
        self.pending_layer_key = None;
        self.pending_layer_score = 0;
        self.pause_start_ts = None;
        self.long_pause_pending = false;
        self.just_resumed_from_pause = false;
        self.resume_frames_left = 0;
        self.relocate_tier2_left = 0;
        self.consecutive_sift_misses = 0;
        self.sift_interframe_budget = 0;
        self.global_vote_bins.clear();
        self.global_query_count = 0;
        self.last_sift_inliers = 0;
        self.gate.reset_keyframe();
        Ok(self.build_pose(ts, false, "map_m_anchor", None, true, 1.0, 0))
    }

    pub fn pause(
        &mut self,
        now: Option<f64>,
        reason: &str,
        paimon_score: f64,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;
        self.fsm.pause(ts)?;
        if self.pause_start_ts.is_none() {
            self.pause_start_ts = Some(ts);
            self.global_vote_bins.clear();
            self.global_query_count = 0;
        }
        if let Some(ps) = self.pause_start_ts
            && (ts - ps) >= 1.2
        {
            self.long_pause_pending = true;
            self.relocate_tier2_left = 2;
        }
        self.just_resumed_from_pause = true;
        self.resume_frames_left = 8;
        self.sift_interframe_budget = 0;
        self.prev_phase_gray = None;
        self.prev_mm_gray_for_phase = None;
        self.gate.reset_keyframe();
        let method = if reason.contains("paimon") {
            "paimon_pause"
        } else {
            "paused"
        };
        Ok(self.build_pose(
            ts,
            false,
            method,
            Some(reason.to_owned()),
            false,
            paimon_score,
            0,
        ))
    }

    pub fn on_loading_screen(&mut self, now: Option<f64>) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;
        if self.pause_start_ts.is_none() {
            self.pause_start_ts = Some(ts);
        }
        self.long_pause_pending = true;
        self.relocate_tier2_left = 2;
        self.just_resumed_from_pause = true;
        self.resume_frames_left = 10;
        self.sift_interframe_budget = 0;
        self.pending_layer_key = None;
        self.pending_layer_score = 0;
        self.global_vote_bins.clear();
        self.global_query_count = 0;
        self.prev_phase_gray = None;
        self.prev_mm_gray_for_phase = None;
        self.gate.reset_keyframe();
        self.fsm.pause(ts)?;
        Ok(self.build_pose(
            ts,
            false,
            "loading_pause",
            Some("loading_screen".to_owned()),
            false,
            0.0,
            0,
        ))
    }

    pub fn ingest_measurement(
        &mut self,
        measurement: PoseMeasurement,
        now: Option<f64>,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;
        let (acc, _, reason) = self.fsm.update(&measurement, ts)?;
        if measurement.found && measurement.match_method != "motion" {
            self.motion_fallback_frames = 0;
        }
        Ok(self.build_pose(
            ts,
            false,
            &measurement.match_method,
            reason,
            acc,
            1.0,
            0,
        ))
    }

    #[inline]
    fn parabolic_peak_offset(v_prev: f64, v_curr: f64, v_next: f64) -> f64 {
        let denom = v_prev - 2.0 * v_curr + v_next;
        if denom.abs() > 1e-6 {
            (0.5 * (v_prev - v_next) / denom).clamp(-0.5, 0.5)
        } else {
            0.0
        }
    }

    fn get_cone_masked_annulus(
        &mut self,
        size: usize,
        exclude_cone_cw_deg: Option<f64>,
        half_fov_deg: f64,
    ) -> Array2<u8> {
        let mut mask = self.get_annulus_mask(size).clone();
        if let Some(ang) = exclude_cone_cw_deg
            && ang.is_finite()
        {
            let c = (size / 2) as f64;
            let cv_ang = wrap_angle_360(ang) - 90.0;
            let sz_f = size as f64;
            draw_filled_ellipse_sector_u8(
                &mut mask,
                c,
                c,
                sz_f,
                sz_f,
                cv_ang - half_fov_deg,
                cv_ang + half_fov_deg,
                0,
            );
        }
        mask
    }

    #[allow(clippy::too_many_arguments)]
    fn match_annulus_template(
        &mut self,
        minimap_gray: ArrayView2<'_, u8>,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
        regime: ZoomRegime,
        pre_rotate_cw_deg: f64,
        exclude_cone_cw_deg: Option<f64>,
    ) -> Option<(f64, f64, f64)> {
        let (span_world, search_rad, target_ppu) = self.patch_params_for_regime(regime);
        let half_extent = 0.5 * span_world + search_rad;
        let (ref_patch_opt, meta) =
            self.tile_cache
                .extract_local_patch(map_id, anchor_x, anchor_y, half_extent, target_ppu);
        let ref_patch = ref_patch_opt?;
        if ref_patch.is_empty() || std_f64_u8(ref_patch.as_slice().unwrap()) < 3.0 {
            return None;
        }

        let ds = self.config.match_downscale;
        let (ref_h, ref_w) = ref_patch.dim();
        let ds_w = round_usize_even(ref_w as f64 * ds).max(24);
        let ds_h = round_usize_even(ref_h as f64 * ds).max(24);
        let eff_ppu_x = meta.px_per_unit * (ds_w as f64 / ref_w as f64);
        let eff_ppu_y = meta.px_per_unit * (ds_h as f64 / ref_h as f64);
        let ref_ds = resize_u8(ref_patch.view(), ds_w, ds_h, Interpolation::Area).ok()?;

        let mm_rot_owned;
        let mm_work = if pre_rotate_cw_deg.abs() > 0.25 {
            let (mh, mw) = minimap_gray.dim();
            let m_rot = get_rotation_matrix_2d(
                ((mw - 1) as f64 * 0.5, (mh - 1) as f64 * 0.5),
                -pre_rotate_cw_deg,
                1.0,
            );
            mm_rot_owned = warp_affine_u8(
                minimap_gray,
                &m_rot,
                mw,
                mh,
                Interpolation::Linear,
                false,
                BorderMode::Replicate,
            )
            .ok()?;
            mm_rot_owned.view()
        } else {
            minimap_gray
        };

        let tpl_sz = round_usize_even(span_world * eff_ppu_x).max(20);
        if tpl_sz >= ds_w.saturating_sub(2) || tpl_sz >= ds_h.saturating_sub(2) {
            return None;
        }
        let tpl_ds = resize_u8(mm_work, tpl_sz, tpl_sz, Interpolation::Area).ok()?;
        let eff_cone = exclude_cone_cw_deg.map(|c| wrap_angle_360(c + pre_rotate_cw_deg));
        let mut mask = self.get_cone_masked_annulus(tpl_sz, eff_cone, 52.0);

        let mut base_cnt = 0usize;
        let mut non_bright_cnt = 0usize;
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    base_cnt += 1;
                    if tpl_ds[[y, x]] < 235 {
                        non_bright_cnt += 1;
                    }
                }
            }
        }
        if non_bright_cnt >= (0.65 * base_cnt.max(1) as f64) as usize {
            for y in 0..tpl_sz {
                for x in 0..tpl_sz {
                    if tpl_ds[[y, x]] >= 235 {
                        mask[[y, x]] = 0;
                    }
                }
            }
        }

        let mut active_vals = Vec::with_capacity(tpl_sz * tpl_sz);
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    active_vals.push(tpl_ds[[y, x]]);
                }
            }
        }
        if std_f64_u8(&active_vals) < 3.0 {
            return None;
        }

        let res = match_template_masked_u8(
            ref_ds.view(),
            tpl_ds.view(),
            mask.view(),
            TemplateMatchMode::CcoeffNormed,
        )
        .ok()?;
        let (_, max_val, _, (mx, my)) = min_max_loc_f32(res.view()).ok()?;
        let corr = max_val as f64;
        if !corr.is_finite() {
            return None;
        }

        let (rh, rw) = res.dim();
        let sx_off = if mx > 0 && mx + 1 < rw {
            Self::parabolic_peak_offset(
                res[[my, mx - 1]] as f64,
                res[[my, mx]] as f64,
                res[[my, mx + 1]] as f64,
            )
        } else {
            0.0
        };
        let sy_off = if my > 0 && my + 1 < rh {
            Self::parabolic_peak_offset(
                res[[my - 1, mx]] as f64,
                res[[my, mx]] as f64,
                res[[my + 1, mx]] as f64,
            )
        } else {
            0.0
        };

        let pred_wx = meta.origin_wx + (mx as f64 + sx_off + 0.5 * tpl_sz as f64) / eff_ppu_x;
        let pred_wy = meta.origin_wy + (my as f64 + sy_off + 0.5 * tpl_sz as f64) / eff_ppu_y;
        Some((pred_wx, pred_wy, corr))
    }

    fn estimate_follow_view_orb(
        &mut self,
        minimap_gray: ArrayView2<'_, u8>,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
        regime: ZoomRegime,
    ) -> Option<(f64, f64, f64, f64, usize)> {
        let (mh, mw) = minimap_gray.dim();
        let mm_sz = mh.min(mw);
        let span_world = self.span_for_regime(regime);
        let search_rad = match regime {
            ZoomRegime::City => self.config.search_radius_city,
            ZoomRegime::OpenWorld => self.config.search_radius_open_world,
        };
        let ppu = mm_sz as f64 / span_world;
        let half_extent = 0.5 * span_world + search_rad;
        let (ref_patch_opt, meta) =
            self.tile_cache
                .extract_local_patch(map_id, anchor_x, anchor_y, half_extent, ppu);
        let ref_patch = ref_patch_opt?;
        if ref_patch.is_empty() || std_f64_u8(ref_patch.as_slice().unwrap()) < 5.0 {
            return None;
        }

        let ref_eq = clahe_u8(ref_patch.view(), 2.0, (8, 8)).ok()?;
        let mm_sq = if mh == mw {
            minimap_gray.to_owned()
        } else {
            resize_u8(minimap_gray, mm_sz, mm_sz, Interpolation::Area).ok()?
        };
        let mm_eq = clahe_u8(mm_sq.view(), 2.0, (8, 8)).ok()?;
        let mm_mask = self.get_annulus_mask(mm_sz).clone();

        let (kp_r, des_r) = self.orb.detect_and_compute(ref_eq.view(), None).ok()?;
        let (kp_m, des_m) = self
            .orb
            .detect_and_compute(mm_eq.view(), Some(mm_mask.view()))
            .ok()?;
        if kp_r.len() < 8 || kp_m.len() < 8 {
            return None;
        }

        let raw_matches = bf_knn_match_hamming(&des_r, &des_m, 2);
        let mut pts_r = Vec::new();
        let mut pts_m = Vec::new();
        for pair in raw_matches {
            if pair.len() == 2 && pair[0].distance < 0.78 * pair[1].distance {
                let kr = kp_r[pair[0].query_idx];
                let km = kp_m[pair[0].train_idx];
                pts_r.push([kr.x, kr.y]);
                pts_m.push([km.x, km.y]);
            }
        }
        let n_good = pts_r.len();
        if n_good < 4usize.max(self.config.follow_view_min_inliers) {
            return None;
        }

        let (m, inliers) = estimate_affine_partial_2d_ransac(&pts_r, &pts_m, 4.0, 2000, 0.99)?;
        let inlier_count: usize = inliers.iter().map(|&v| if v > 0 { 1 } else { 0 }).sum();
        if inlier_count < self.config.follow_view_min_inliers {
            return None;
        }

        let scale_est = m[0].hypot(m[3]);
        if !(0.72..=1.38).contains(&scale_est) {
            return None;
        }
        let theta_map_deg = m[3].atan2(m[0]).to_degrees();
        let m_inv = invert_affine_transform(&m)?;
        let mm_cx = (mm_sz - 1) as f64 * 0.5;
        let mm_cy = (mm_sz - 1) as f64 * 0.5;
        let ref_cx = m_inv[0] * mm_cx + m_inv[1] * mm_cy + m_inv[2];
        let ref_cy = m_inv[3] * mm_cx + m_inv[4] * mm_cy + m_inv[5];
        let pred_wx = meta.origin_wx + (ref_cx + 0.5) / meta.px_per_unit;
        let pred_wy = meta.origin_wy + (ref_cy + 0.5) / meta.px_per_unit;

        let inlier_ratio = inlier_count as f64 / (n_good.max(1) as f64);
        let orb_conf = (0.45 + 0.35 * (inlier_count as f64 / 40.0).min(1.0) + 0.15 * inlier_ratio)
            .clamp(0.50, 0.95);
        Some((pred_wx, pred_wy, theta_map_deg, orb_conf, inlier_count))
    }

    fn prepare_phase_patch(
        &self,
        minimap_gray: ArrayView2<'_, u8>,
        exclude_cone_cw_deg: Option<f64>,
    ) -> Option<Array2<f32>> {
        let (h, w) = minimap_gray.dim();
        let y0 = round_usize_even(0.20 * h as f64).min(h.saturating_sub(1));
        let y1 = round_usize_even(0.80 * h as f64).clamp(y0 + 1, h);
        let x0 = round_usize_even(0.20 * w as f64).min(w.saturating_sub(1));
        let x1 = round_usize_even(0.80 * w as f64).clamp(x0 + 1, w);
        let crop = minimap_gray.slice(s![y0..y1, x0..x1]);
        let resized_u8 =
            resize_u8(crop, self.phase_size, self.phase_size, Interpolation::Area).ok()?;
        let mut resized = resized_u8.mapv(|v| v as f32);
        let mean_val =
            (resized.iter().map(|&v| v as f64).sum::<f64>() / (resized.len().max(1) as f64)) as f32;
        let c = (self.phase_size / 2) as i32;
        let rad = round_usize_even(0.26 * c as f64) as i32;
        draw_filled_circle_f32(&mut resized, c, c, rad, mean_val);
        if let Some(ang) = exclude_cone_cw_deg
            && ang.is_finite()
        {
            let cv_ang = wrap_angle_360(ang) - 90.0;
            let sz_f = self.phase_size as f64;
            draw_filled_ellipse_sector_f32(
                &mut resized,
                c as f64,
                c as f64,
                sz_f,
                sz_f,
                cv_ang - 48.0,
                cv_ang + 48.0,
                mean_val,
            );
        }
        Some(resized)
    }

    fn run_phase_correlate_fallback(&self, curr_phase: ArrayView2<'_, f32>) -> Option<(f64, f64, f64)> {
        let (Some(prev_phase), Some(fx), Some(fy)) =
            (self.prev_phase_gray.as_ref(), self.fsm.x, self.fsm.y)
        else {
            return None;
        };
        if curr_phase.dim() != prev_phase.dim() {
            return None;
        }
        let ((dx, dy), resp) =
            phase_correlate(prev_phase.view(), curr_phase, Some(self.hann_window.view())).ok()?;
        if !dx.is_finite() || !dy.is_finite() || !resp.is_finite() {
            return None;
        }
        if resp < self.config.phase_min_response {
            return None;
        }
        let shift_mag = dx.hypot(dy);
        if shift_mag > self.config.phase_max_shift_frac * (self.phase_size as f64) {
            return None;
        }
        let span_world = if self.span_confirmed {
            self.confirmed_span_world
        } else {
            self.span_for_regime(self.zoom_regime)
        };
        let world_units_per_phase_px = (0.60 * span_world) / (self.phase_size as f64);
        let step_mx = -dx * world_units_per_phase_px;
        let step_my = -dy * world_units_per_phase_px;
        let (step_wx, step_wy) =
            if self.minimap_mode == MinimapMode::FollowView && self.map_rotation_deg.abs() > 0.5 {
                let rad = (-self.map_rotation_deg).to_radians();
                let (s, c) = rad.sin_cos();
                (c * step_mx - s * step_my, s * step_mx + c * step_my)
            } else {
                (step_mx, step_my)
            };
        let conf = (0.34 + 0.30 * resp).clamp(0.36, 0.58);
        Some((fx + step_wx, fy + step_wy, conf))
    }

    #[inline]
    fn cone_mask_heading(&self) -> Option<f64> {
        self.cone_raw_heading.or(self.camera_heading)
    }

    fn update_headings_from_color(
        &mut self,
        minimap_bgr_or_bgra: Option<ArrayView3<'_, u8>>,
        frame_diff: f64,
        alpha_diff: f64,
        center_diff: f64,
        ts: f64,
    ) -> (Option<f64>, Option<f64>) {
        let arrow_diff_thresh = if self.body_heading.is_some() {
            self.config.arrow_gate_change_diff
        } else {
            self.config.gate_cone_change_diff
        };
        let arrow_may_have_changed =
            self.body_heading.is_none() || center_diff >= arrow_diff_thresh;
        if arrow_may_have_changed && let Some(mm) = minimap_bgr_or_bgra {
            let det = extract_player_arrow_360(mm);
            if let Some(deg) = det.heading_deg
                && det.confidence > 0.0
            {
                self.body_heading = self.body_smoother.update(Some(deg));
                self.body_conf = det.confidence;
            }
        }

        let cone_may_have_changed = self.camera_heading.is_none()
            || frame_diff >= self.config.gate_cone_change_diff
            || alpha_diff >= self.config.gate_cone_change_diff;
        let mut cam_deg = None;
        let mut cam_conf = 0.0;
        let mut cam_method = crate::heading360::ConeMethod::None;
        if cone_may_have_changed && let Some(mm) = minimap_bgr_or_bgra {
            let det = extract_camera_view_cone(mm);
            if let Some(deg) = det.heading_deg
                && det.confidence > 0.0
            {
                self.cone_raw_heading = Some(deg);
            }
            cam_deg = det.heading_deg;
            cam_conf = det.confidence;
            cam_method = det.method;
        }

        let prev_source = self.camera_fuser.source();
        let fused = self.camera_fuser.update(
            if cone_may_have_changed { cam_deg } else { None },
            cam_conf,
            cam_method,
            self.body_heading,
            self.body_conf,
            ts,
        );
        if let Some(f_deg) = fused {
            let changed = self
                .camera_heading
                .is_none_or(|ch| shortest_angle_delta(ch, f_deg).abs() > 0.05);
            if changed || self.camera_fuser.source() != prev_source {
                if self.camera_fuser.source() == FuserSource::Body && prev_source != FuserSource::Body
                {
                    self.camera_smoother.reset();
                }
                self.camera_heading = self.camera_smoother.update(Some(f_deg));
                self.camera_conf = self.camera_fuser.confidence();
                self.camera_method = self.camera_fuser.source();
            }
        }

        if self.minimap_mode == MinimapMode::FollowView {
            if let Some(cam) = self.camera_heading
                && self.camera_conf >= 0.45
                && shortest_angle_delta(0.0, cam).abs() >= self.config.follow_view_unlock_deg
            {
                self.minimap_mode = MinimapMode::LockDirection;
                self.map_rotation_deg = 0.0;
                self.view_heading = self.camera_heading;
            } else {
                self.view_heading = Some(wrap_angle_360(-self.map_rotation_deg));
            }
        } else if self.camera_heading.is_some() && self.camera_conf > 0.0 {
            self.view_heading = self.camera_heading;
        } else if self.body_heading.is_some() && self.body_conf > 0.0 {
            self.view_heading = self.body_heading;
        }

        (self.camera_heading, self.body_heading)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn compare_surface_vs_underground(
        &mut self,
        minimap: FrameView<'_>,
        surface_patch: Option<ArrayView2<'_, u8>>,
        underground_patch: Option<ArrayView2<'_, u8>>,
        candidate_floor_id: Option<i32>,
        candidate_floor_name: &str,
        patch_px_per_unit: Option<f64>,
        apply_hysteresis: bool,
    ) -> FloorCompareResult {
        let Ok(mm_gray_raw) = frame_view_to_gray_or_bgr_gray(minimap) else {
            return FloorCompareResult {
                surface_score: 0.0,
                underground_score: 0.0,
                margin: 0.0,
                is_underground: self.fsm.is_underground,
                floor_id: self.fsm.floor_id,
                floor_name: self.fsm.floor_name.clone(),
                streak: self.floor_switch_streak,
                switched: false,
            };
        };
        self.compare_surface_vs_underground_gray(
            mm_gray_raw.view(),
            surface_patch,
            underground_patch,
            candidate_floor_id,
            candidate_floor_name,
            patch_px_per_unit,
            apply_hysteresis,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn compare_surface_vs_underground_gray(
        &mut self,
        minimap_gray: ArrayView2<'_, u8>,
        surface_patch: Option<ArrayView2<'_, u8>>,
        underground_patch: Option<ArrayView2<'_, u8>>,
        candidate_floor_id: Option<i32>,
        candidate_floor_name: &str,
        patch_px_per_unit: Option<f64>,
        apply_hysteresis: bool,
    ) -> FloorCompareResult {
        if minimap_gray.is_empty() {
            return FloorCompareResult {
                surface_score: 0.0,
                underground_score: 0.0,
                margin: 0.0,
                is_underground: self.fsm.is_underground,
                floor_id: self.fsm.floor_id,
                floor_name: self.fsm.floor_name.clone(),
                streak: self.floor_switch_streak,
                switched: false,
            };
        }

        let mm_rot_owned;
        let mm_gray = if self.minimap_mode == MinimapMode::FollowView
            && self.map_rotation_deg.abs() > 0.5
        {
            let (mh, mw) = minimap_gray.dim();
            let m_rot = get_rotation_matrix_2d(
                ((mw - 1) as f64 * 0.5, (mh - 1) as f64 * 0.5),
                -self.map_rotation_deg,
                1.0,
            );
            mm_rot_owned = warp_affine_u8(
                minimap_gray,
                &m_rot,
                mw,
                mh,
                Interpolation::Linear,
                false,
                BorderMode::Replicate,
            )
            .unwrap_or_else(|_| minimap_gray.to_owned());
            mm_rot_owned.view()
        } else {
            minimap_gray
        };

        let span = self.span_for_regime(self.zoom_regime);
        let surf_score = self.score_layer_patch(mm_gray, surface_patch, span, patch_px_per_unit);
        let ug_score = self.score_layer_patch(mm_gray, underground_patch, span, patch_px_per_unit);

        let mut switched = false;
        if apply_hysteresis {
            let margin_req = self.config.floor_switch_margin;
            let min_score = self.config.floor_switch_min_score;
            let wins_req = self.config.floor_switch_consecutive_wins;

            if !self.fsm.is_underground {
                if ug_score >= min_score && (ug_score - surf_score) >= margin_req {
                    if candidate_floor_id == self.floor_switch_candidate_id {
                        self.floor_switch_streak += 1;
                    } else {
                        self.floor_switch_candidate_id = candidate_floor_id;
                        self.floor_switch_streak = 1;
                    }
                    if self.floor_switch_streak >= wins_req {
                        self.fsm
                            .set_floor(true, candidate_floor_id, candidate_floor_name);
                        self.floor_switch_streak = 0;
                        self.floor_switch_candidate_id = None;
                        switched = true;
                    }
                } else {
                    self.floor_switch_streak = 0;
                    self.floor_switch_candidate_id = None;
                }
            } else if surf_score >= min_score && (surf_score - ug_score) >= margin_req {
                self.floor_switch_streak += 1;
                if self.floor_switch_streak >= wins_req {
                    self.fsm.set_floor(false, None, "G");
                    self.floor_switch_streak = 0;
                    self.floor_switch_candidate_id = None;
                    switched = true;
                }
            } else {
                self.floor_switch_streak = 0;
            }
        }

        FloorCompareResult {
            surface_score: surf_score,
            underground_score: ug_score,
            margin: ug_score - surf_score,
            is_underground: self.fsm.is_underground,
            floor_id: self.fsm.floor_id,
            floor_name: self.fsm.floor_name.clone(),
            streak: self.floor_switch_streak,
            switched,
        }
    }

    fn score_layer_patch(
        &mut self,
        mm_gray: ArrayView2<'_, u8>,
        patch: Option<ArrayView2<'_, u8>>,
        span_world: f64,
        patch_px_per_unit: Option<f64>,
    ) -> f64 {
        let Some(p_gray) = patch else {
            return 0.0;
        };
        let (ph, pw) = p_gray.dim();
        if ph.min(pw) < 20 {
            return 0.0;
        }
        let p_owned = p_gray.as_standard_layout().into_owned();
        if std_f64_u8(p_owned.as_slice().unwrap()) < 3.0 {
            return 0.0;
        }

        let (mh, mw) = mm_gray.dim();
        let tpl_sz = if let Some(ppu) = patch_px_per_unit
            && ppu > 0.0
        {
            round_usize_even(span_world * ppu).max(20).min(ph.min(pw))
        } else {
            ph.min(pw).min(mh.min(mw))
        }
        .max(20);

        let Ok(mm_tpl) = resize_u8(mm_gray, tpl_sz, tpl_sz, Interpolation::Area) else {
            return 0.0;
        };
        let mask = self.get_annulus_mask(tpl_sz).clone();
        let Ok(mm_edge) = sobel_magnitude_u8_f32(mm_tpl.view()) else {
            return 0.0;
        };
        let Ok(p_edge) = sobel_magnitude_u8_f32(p_gray) else {
            return 0.0;
        };

        let mut mm_act = Vec::new();
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    mm_act.push(mm_edge[[y, x]]);
                }
            }
        }
        if std_f64_f32(&mm_act) < 1e-3 {
            return 0.0;
        }
        if ph == tpl_sz && pw == tpl_sz {
            let mut p_act = Vec::new();
            for y in 0..tpl_sz {
                for x in 0..tpl_sz {
                    if mask[[y, x]] > 0 {
                        p_act.push(p_edge[[y, x]]);
                    }
                }
            }
            if std_f64_f32(&p_act) < 1e-3 {
                return 0.0;
            }
        }

        let Ok(res) = match_template_masked_f32(
            p_edge.view(),
            mm_edge.view(),
            mask.view(),
            TemplateMatchMode::CcoeffNormed,
        ) else {
            return 0.0;
        };
        let Ok((_, max_val, _, _)) = min_max_loc_f32(res.view()) else {
            return 0.0;
        };
        if !max_val.is_finite() {
            return 0.0;
        }
        (max_val as f64).clamp(0.0, 1.0)
    }

    fn extract_ug_patch_from_provider(
        &mut self,
        map_id: i32,
        floor_id: i32,
        cx_cell: f64,
        cy_cell: f64,
        half_ext: f64,
        ppu: f64,
    ) -> (Option<Array2<u8>>, crate::tile_cache::PatchMeta) {
        let default_meta = crate::tile_cache::PatchMeta {
            origin_wx: cx_cell,
            origin_wy: cy_cell,
            px_per_unit: ppu,
        };
        let (Some(db), Some(ug)) = (self.multimap_db.as_ref(), self.ug_provider.as_ref()) else {
            return (None, default_meta);
        };
        let Some(rec) = db.get_floor_overlay(map_id, floor_id, None) else {
            return (None, default_meta);
        };
        let okey = UgOverlayKey {
            map_id,
            group_id: rec.group_id,
            floor_id: rec.floor_id,
        };

        if let Some(pos) = self.ug_overlay_cache.iter().position(|(k, _)| *k == okey) {
            let entry = self.ug_overlay_cache.remove(pos).unwrap();
            let res = extract_underground_patch_gray(
                entry.1.gray.view(),
                &entry.1.bounds,
                cx_cell,
                cy_cell,
                half_ext,
                ppu,
                None,
            );
            self.ug_overlay_cache.push_back(entry);
            return res;
        }

        let floor_key = format!("{}:{}", rec.group_id, rec.floor_id);
        let bytes = ug
            .get_overlay_bytes(map_id, &floor_key, true)
            .or_else(|| ug.get_overlay_bytes(map_id, &rec.floor_id.to_string(), true));
        let Some(png_bytes) = bytes else {
            return (None, default_meta);
        };
        let Ok(gray) = overlay_bytes_to_gray(&png_bytes) else {
            return (None, default_meta);
        };
        let bounds = OverlayBounds::from_floor_overlay_meta(&rec.overlay);
        let res = extract_underground_patch_gray(
            gray.view(),
            &bounds,
            cx_cell,
            cy_cell,
            half_ext,
            ppu,
            None,
        );
        self.ug_overlay_cache
            .push_back((okey, CachedUgOverlay { gray, bounds }));
        while self.ug_overlay_cache.len() > self.max_ug_overlays {
            self.ug_overlay_cache.pop_front();
        }
        res
    }

    fn get_or_build_ref_cell(
        &mut self,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
        is_underground: bool,
        floor_id: Option<i32>,
        zoom_tier: i32,
    ) -> Option<usize> {
        let z_eff = if !is_underground && zoom_tier == 0 {
            0
        } else {
            -1
        };
        let (step_w, ppu, half_ext) = if z_eff == 0 {
            (96.0, 1.0, 176.0)
        } else {
            (128.0, 0.5, 280.0)
        };
        let cx_cell = (anchor_x / step_w).round_ties_even() * step_w;
        let cy_cell = (anchor_y / step_w).round_ties_even() * step_w;
        let fid_key = if is_underground { floor_id } else { None };
        let ckey = RefCellKey {
            map_id,
            is_underground,
            floor_id: fid_key,
            zoom_tier: z_eff,
            cx: cx_cell as i64,
            cy: cy_cell as i64,
        };
        let dl_seq = self.tile_cache.provider_load_seq();

        if let Some(pos) = self.cell_cache.iter().position(|(k, _)| *k == ckey) {
            if self.cell_cache[pos].1.dl_seq == dl_seq {
                let entry = self.cell_cache.remove(pos).unwrap();
                self.cell_cache.push_back(entry);
                return Some(self.cell_cache.len() - 1);
            }
            self.cell_cache.remove(pos);
        }

        let (patch_opt, meta) = if is_underground {
            let fid = floor_id?;
            self.extract_ug_patch_from_provider(map_id, fid, cx_cell, cy_cell, half_ext, ppu)
        } else {
            self.tile_cache
                .extract_local_patch(map_id, cx_cell, cy_cell, half_ext, ppu)
        };
        let patch = patch_opt?;
        if std_f64_u8(patch.as_slice().unwrap()) < 4.0 {
            return None;
        }
        let eq = clahe_u8(patch.view(), 2.0, (8, 8)).ok()?;
        let (kp, des) = self.sift_ref.detect_and_compute(eq.view(), None).ok()?;
        let sobel = sobel_magnitude_u8_f32(patch.view()).ok()?;
        let rec = RefCellRecord {
            sobel,
            sobel_half: None,
            kp,
            des,
            ox: meta.origin_wx,
            oy: meta.origin_wy,
            ppu: meta.px_per_unit,
            dl_seq,
        };
        self.cell_cache.push_back((ckey, rec));
        while self.cell_cache.len() > self.max_cells {
            self.cell_cache.pop_front();
        }
        Some(self.cell_cache.len() - 1)
    }

    fn extract_mm_sift(
        &mut self,
        mm_gray: ArrayView2<'_, u8>,
        mm_bgr: Option<ArrayView3<'_, u8>>,
        sz: usize,
    ) -> (Vec<KeyPoint>, Vec<[f32; 128]>, usize) {
        let (h, w) = mm_gray.dim();
        let use_sz = sz.min(h.min(w));
        if use_sz < 16 {
            return (Vec::new(), Vec::new(), use_sz);
        }
        let sq_g = if h == use_sz && w == use_sz {
            mm_gray.to_owned()
        } else {
            let Ok(r) = resize_u8(mm_gray, use_sz, use_sz, Interpolation::Area) else {
                return (Vec::new(), Vec::new(), use_sz);
            };
            r
        };
        let mut mask = self.get_annulus_mask(use_sz).clone();
        if let Some(bgr_view) = mm_bgr {
            let (bh, bw, bc) = bgr_view.dim();
            if bc >= 3 {
                let bgr3 = bgr_view.slice(s![.., .., 0..3]);
                if let Ok(sq_bgr) = if bh == use_sz && bw == use_sz {
                    Ok(bgr3.to_owned())
                } else {
                    resize_u8_hwc(bgr3, use_sz, use_sz, Interpolation::Area)
                } && let Ok(hsv) = bgr_to_hsv(sq_bgr.view())
                {
                    let mut active_px = 0usize;
                    let mut white_in_mask = 0usize;
                    let mut flags = vec![false; use_sz * use_sz];
                    for y in 0..use_sz {
                        for x in 0..use_sz {
                            if mask[[y, x]] > 0 {
                                active_px += 1;
                                if sq_g[[y, x]] >= 238 && hsv[[y, x, 1]] <= 38 {
                                    white_in_mask += 1;
                                    flags[y * use_sz + x] = true;
                                }
                            }
                        }
                    }
                    if active_px > 0
                        && (active_px - white_in_mask) as f64 >= 0.4 * active_px as f64
                    {
                        for y in 0..use_sz {
                            for x in 0..use_sz {
                                if flags[y * use_sz + x] {
                                    mask[[y, x]] = 0;
                                }
                            }
                        }
                    }
                }
            }
        }
        let Ok(eq) = clahe_u8(sq_g.view(), 2.0, (8, 8)) else {
            return (Vec::new(), Vec::new(), use_sz);
        };
        match self.sift_mm.detect_and_compute(eq.view(), Some(mask.view())) {
            Ok((kp, des)) => (kp, des, use_sz),
            Err(_) => (Vec::new(), Vec::new(), use_sz),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn match_sift_feats(
        &self,
        kp_m: &[KeyPoint],
        des_m: &[[f32; 128]],
        sz: usize,
        cell_idx: usize,
        anchor_x: f64,
        anchor_y: f64,
        max_dist_w: f64,
    ) -> Option<(f64, f64, usize, f64, f64)> {
        let cell = &self.cell_cache[cell_idx].1;
        let min_inl = self.config.sift_min_inliers;
        if cell.kp.len() < 10 || cell.des.is_empty() || kp_m.len() < min_inl || des_m.is_empty() {
            return None;
        }
        let pairs = bf_knn_match_l2(des_m, &cell.des, 2);
        let good = lowe_ratio_filter(&pairs, 0.78);
        if good.len() < min_inl {
            return None;
        }
        let pts_m: Vec<[f32; 2]> = good
            .iter()
            .map(|m| [kp_m[m.query_idx].x, kp_m[m.query_idx].y])
            .collect();
        let pts_r: Vec<[f32; 2]> = good
            .iter()
            .map(|m| [cell.kp[m.train_idx].x, cell.kp[m.train_idx].y])
            .collect();
        let (m, inl) = estimate_affine_partial_2d_ransac(&pts_m, &pts_r, 3.5, 1200, 0.99)?;
        let ninl: usize = inl.iter().map(|&v| if v > 0 { 1 } else { 0 }).sum();
        if ninl < min_inl {
            return None;
        }
        let sc = m[0].hypot(m[3]);
        let theta_mm_to_ref = m[3].atan2(m[0]).to_degrees();
        let theta_map_deg = ((-theta_mm_to_ref) + 180.0).rem_euclid(360.0) - 180.0;
        let span_w = (sz as f64 * sc) / cell.ppu;
        if !(95.0..=430.0).contains(&span_w) {
            return None;
        }
        let c = (sz - 1) as f64 * 0.5;
        let rx = m[0] * c + m[1] * c + m[2];
        let ry = m[3] * c + m[4] * c + m[5];
        let wx = cell.ox + (rx + 0.5) / cell.ppu;
        let wy = cell.oy + (ry + 0.5) / cell.ppu;
        if (wx - anchor_x).hypot(wy - anchor_y) > max_dist_w {
            return None;
        }
        Some((wx, wy, ninl, span_w, theta_map_deg))
    }

    #[allow(clippy::too_many_arguments)]
    fn match_sobel_fast(
        &mut self,
        mm_gray: ArrayView2<'_, u8>,
        cell_idx: usize,
        anchor_x: f64,
        anchor_y: f64,
        span_world: f64,
        search_rad_w: f64,
        pre_rotate_cw_deg: f64,
        exclude_cone_cw_deg: Option<f64>,
    ) -> Option<(f64, f64, f64)> {
        let (ppu, cell_ox, cell_oy, sobel_ref, eff_ppu) = {
            let cell = &mut self.cell_cache[cell_idx].1;
            let ppu = cell.ppu;
            let tpl_sz_native = round_usize_even(span_world * ppu).max(24);
            let (full_h, full_w) = cell.sobel.dim();
            let (s_ref, e_ppu) = if tpl_sz_native > 96 {
                if cell.sobel_half.is_none() {
                    let hw = (full_w / 2).max(16);
                    let hh = (full_h / 2).max(16);
                    cell.sobel_half =
                        resize_f32(cell.sobel.view(), hw, hh, Interpolation::Area).ok();
                }
                let sh = cell.sobel_half.as_ref()?.clone();
                let ep = ppu * (sh.dim().1 as f64 / full_w as f64);
                (sh, ep)
            } else {
                (cell.sobel.clone(), ppu)
            };
            (ppu, cell.ox, cell.oy, s_ref, e_ppu)
        };
        let _ = ppu;

        let tpl_sz = round_usize_even(span_world * eff_ppu).max(24);
        let ax_px = (anchor_x - cell_ox) * eff_ppu;
        let ay_px = (anchor_y - cell_oy) * eff_ppu;
        let half_need = round_usize_even(0.5 * tpl_sz as f64 + search_rad_w * eff_ppu) + 2;
        let (s_h, s_w) = sobel_ref.dim();
        let x0 = ((ax_px - half_need as f64).floor() as isize).max(0) as usize;
        let y0 = ((ay_px - half_need as f64).floor() as isize).max(0) as usize;
        let x1 = s_w.min(((ax_px + half_need as f64).ceil() as isize).max(0) as usize);
        let y1 = s_h.min(((ay_px + half_need as f64).ceil() as isize).max(0) as usize);
        if x1.saturating_sub(x0) <= tpl_sz + 2 || y1.saturating_sub(y0) <= tpl_sz + 2 {
            return None;
        }
        let roi_edge = sobel_ref.slice(s![y0..y1, x0..x1]);

        let mm_rot_owned;
        let mm_work = if pre_rotate_cw_deg.abs() > 0.25 {
            let (mh, mw) = mm_gray.dim();
            let m_rot = get_rotation_matrix_2d(
                ((mw - 1) as f64 * 0.5, (mh - 1) as f64 * 0.5),
                -pre_rotate_cw_deg,
                1.0,
            );
            mm_rot_owned = warp_affine_u8(
                mm_gray,
                &m_rot,
                mw,
                mh,
                Interpolation::Linear,
                false,
                BorderMode::Replicate,
            )
            .ok()?;
            mm_rot_owned.view()
        } else {
            mm_gray
        };

        let tpl_mm = if mm_work.dim() == (tpl_sz, tpl_sz) {
            mm_work.to_owned()
        } else {
            resize_u8(mm_work, tpl_sz, tpl_sz, Interpolation::Area).ok()?
        };
        let tpl_edge = sobel_magnitude_u8_f32(tpl_mm.view()).ok()?;

        let eff_cone = exclude_cone_cw_deg.map(|c| wrap_angle_360(c + pre_rotate_cw_deg));
        let mut mask = self.get_cone_masked_annulus(tpl_sz, eff_cone, 50.0);
        let mut base_cnt = 0usize;
        let mut non_bright_cnt = 0usize;
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    base_cnt += 1;
                    if tpl_mm[[y, x]] < 236 {
                        non_bright_cnt += 1;
                    }
                }
            }
        }
        if non_bright_cnt >= (0.55 * base_cnt.max(1) as f64) as usize {
            for y in 0..tpl_sz {
                for x in 0..tpl_sz {
                    if tpl_mm[[y, x]] >= 236 {
                        mask[[y, x]] = 0;
                    }
                }
            }
        }

        let mut act_vals = Vec::with_capacity(tpl_sz * tpl_sz);
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    act_vals.push(tpl_edge[[y, x]]);
                }
            }
        }
        let acnt = act_vals.len();
        if acnt < 16 || std_f64_f32(&act_vals) < 1.0 {
            return None;
        }
        let mean_e = (act_vals.iter().map(|&v| v as f64).sum::<f64>() / acnt as f64) as f32;
        let mut tpl_zm = Array2::<f32>::zeros((tpl_sz, tpl_sz));
        for y in 0..tpl_sz {
            for x in 0..tpl_sz {
                if mask[[y, x]] > 0 {
                    tpl_zm[[y, x]] = tpl_edge[[y, x]] - mean_e;
                }
            }
        }

        let res = match_template_f32(roi_edge, tpl_zm.view(), TemplateMatchMode::CcoeffNormed).ok()?;
        let (_, raw_mv_f32, _, (mx, my)) = min_max_loc_f32(res.view()).ok()?;
        let raw_mv = raw_mv_f32 as f64;
        if !raw_mv.is_finite() {
            return None;
        }
        let scale_c = (acnt as f64 / (tpl_sz * tpl_sz) as f64).sqrt();
        let corr = (raw_mv / scale_c.max(0.25)).min(1.0);
        let (rh, rw) = res.dim();
        let sub_x = if mx > 0 && mx + 1 < rw {
            Self::parabolic_peak_offset(
                res[[my, mx - 1]] as f64,
                raw_mv,
                res[[my, mx + 1]] as f64,
            )
        } else {
            0.0
        };
        let sub_y = if my > 0 && my + 1 < rh {
            Self::parabolic_peak_offset(
                res[[my - 1, mx]] as f64,
                raw_mv,
                res[[my + 1, mx]] as f64,
            )
        } else {
            0.0
        };

        let wx = cell_ox + (x0 as f64 + mx as f64 + sub_x + 0.5 * tpl_sz as f64) / eff_ppu;
        let wy = cell_oy + (y0 as f64 + my as f64 + sub_y + 0.5 * tpl_sz as f64) / eff_ppu;
        Some((wx, wy, corr))
    }

    fn apply_rotation_mode(&mut self, theta_map_deg: f64, ninl: usize) {
        if theta_map_deg.abs() <= self.config.follow_view_min_map_rot_deg.max(10.0) {
            self.minimap_mode = MinimapMode::LockDirection;
            self.map_rotation_deg = 0.0;
            self.view_heading = self.camera_heading.or(self.body_heading);
        } else if ninl >= self.config.follow_view_min_inliers.max(8) {
            self.minimap_mode = MinimapMode::FollowView;
            self.map_rotation_deg = theta_map_deg;
            self.view_heading = Some(wrap_angle_360(-theta_map_deg));
        }
    }

    fn update_zoom_from_span(&mut self, span_w: f64) {
        if !self.span_confirmed {
            self.confirmed_span_world = span_w;
            self.span_confirmed = true;
        } else {
            self.confirmed_span_world = 0.65 * self.confirmed_span_world + 0.35 * span_w;
        }
        let want_regime = if span_w < 230.0 {
            ZoomRegime::City
        } else {
            ZoomRegime::OpenWorld
        };
        if want_regime != self.zoom_regime {
            self.zoom_alt_streak += 1;
            if self.zoom_alt_streak >= self.config.zoom_switch_consecutive_wins {
                self.zoom_regime = want_regime;
                self.zoom_alt_streak = 0;
            }
        } else {
            self.zoom_alt_streak = 0;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn probe_candidate_layers(
        &mut self,
        kp_m: &[KeyPoint],
        des_m: &[[f32; 128]],
        mm_sz: usize,
        mm_gray: ArrayView2<'_, u8>,
        cone_excl: f64,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
        cur_is_ug: bool,
        cur_floor_id: Option<i32>,
        cur_ninl: usize,
        max_dist: f64,
        was_resumed: bool,
        ts: f64,
        p_score: f64,
    ) -> Option<MinimapPose> {
        let (Some(db), Some(_)) = (self.multimap_db.as_ref(), self.ug_provider.as_ref()) else {
            return None;
        };
        let overlapping = db.find_overlapping_floors(map_id, anchor_x, anchor_y, 30.0);
        if overlapping.is_empty() {
            self.pending_layer_key = None;
            self.pending_layer_score = 0;
            return None;
        }

        let sort_key = |rec: &FloorOverlayRecord| -> (i32, f64) {
            let inside = rec.lx <= anchor_x
                && anchor_x <= rec.rx
                && rec.ly <= anchor_y
                && anchor_y <= rec.ry;
            let area = (rec.rx - rec.lx) * (rec.ry - rec.ly);
            (if inside { 0 } else { 1 }, area)
        };

        let mut candidates: Vec<(bool, Option<i32>, String)> = Vec::new();
        if !cur_is_ug {
            let mut sorted = overlapping;
            sorted.sort_by(|a, b| {
                let ka = sort_key(a);
                let kb = sort_key(b);
                ka.0.cmp(&kb.0).then_with(|| ka.1.total_cmp(&kb.1))
            });
            for rec in sorted.into_iter().take(3) {
                let fname = if rec.floor_short.is_empty() {
                    rec.floor_name.clone()
                } else {
                    rec.floor_short.clone()
                };
                candidates.push((true, Some(rec.floor_id), fname));
            }
        } else {
            candidates.push((false, None, "G".to_owned()));
            let cur_gid = overlapping
                .iter()
                .find(|r| Some(r.floor_id) == cur_floor_id)
                .map(|r| r.group_id);
            let mut siblings: Vec<FloorOverlayRecord> = overlapping
                .into_iter()
                .filter(|r| {
                    Some(r.floor_id) != cur_floor_id
                        && cur_gid.is_none_or(|gid| r.group_id == gid)
                })
                .collect();
            siblings.sort_by(|a, b| {
                let ka = sort_key(a);
                let kb = sort_key(b);
                ka.0.cmp(&kb.0).then_with(|| ka.1.total_cmp(&kb.1))
            });
            for rec in siblings.into_iter().take(2) {
                let fname = if rec.floor_short.is_empty() {
                    rec.floor_name.clone()
                } else {
                    rec.floor_short.clone()
                };
                candidates.push((true, Some(rec.floor_id), fname));
            }
        }

        let min_cand_ninl = if cur_ninl == 0 {
            8
        } else {
            10usize.max(cur_ninl + 4)
        };
        struct LayerCandidate {
            is_ug: bool,
            floor_id: Option<i32>,
            floor_name: String,
            ninl: usize,
            wx: f64,
            wy: f64,
            span_w: f64,
            theta_map: f64,
        }
        let mut best_cand: Option<LayerCandidate> = None;

        for (c_ug, c_fid, c_fname) in candidates {
            let Some(cell_c_idx) =
                self.get_or_build_ref_cell(map_id, anchor_x, anchor_y, c_ug, c_fid, -1)
            else {
                continue;
            };
            let Some((wx, wy, ninl, span_w, theta_map)) =
                self.match_sift_feats(kp_m, des_m, mm_sz, cell_c_idx, anchor_x, anchor_y, max_dist)
            else {
                continue;
            };
            if ninl < min_cand_ninl {
                continue;
            }
            if best_cand.as_ref().is_none_or(|b| ninl > b.ninl) {
                best_cand = Some(LayerCandidate {
                    is_ug: c_ug,
                    floor_id: c_fid,
                    floor_name: c_fname,
                    ninl,
                    wx,
                    wy,
                    span_w,
                    theta_map,
                });
            }
        }

        let Some(LayerCandidate {
            is_ug: c_ug,
            floor_id: c_fid,
            floor_name: c_fname,
            ninl,
            wx,
            wy,
            span_w,
            theta_map,
        }) = best_cand
        else {
            if cur_ninl >= 10 {
                self.pending_layer_key = None;
                self.pending_layer_score = 0;
            } else {
                self.pending_layer_score = self.pending_layer_score.saturating_sub(1);
                if self.pending_layer_score == 0 {
                    self.pending_layer_key = None;
                }
            }
            return None;
        };

        let lkey = (c_ug, c_fid, c_fname.clone());
        if self.pending_layer_key.as_ref() == Some(&lkey) {
            self.pending_layer_score += 2;
        } else {
            self.pending_layer_key = Some(lkey);
            self.pending_layer_score = 2;
        }

        let immediate_resume_switch = was_resumed && cur_ninl == 0 && ninl >= 13;
        if !(self.pending_layer_score >= 3 || immediate_resume_switch) {
            return None;
        }

        self.pending_layer_key = None;
        self.pending_layer_score = 0;
        self.motion_fallback_frames = 0;
        self.consecutive_sift_misses = 0;
        self.sift_interframe_budget = 0;
        self.just_resumed_from_pause = false;
        self.resume_frames_left = 0;
        self.long_pause_pending = false;
        self.relocate_tier2_left = 0;
        self.global_vote_bins.clear();
        self.global_query_count = 0;
        self.last_zoom_tier = -1;
        self.last_sift_inliers = ninl;
        self.update_zoom_from_span(span_w);
        self.apply_rotation_mode(theta_map, ninl);
        self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
        self.prev_cone_excl_for_phase = Some(cone_excl);
        self.prev_phase_gray = None;
        let _ = self
            .fsm
            .set_anchor(map_id, wx, wy, ts, c_ug, c_fid, &c_fname);
        Some(self.build_pose(
            ts,
            false,
            "layer_switch_sift",
            Some(format!("layer_switch_{c_fname}")),
            true,
            p_score,
            ninl,
        ))
    }

    #[allow(clippy::too_many_arguments)]
    fn probe_tier2_neighbors(
        &mut self,
        kp_m: &[KeyPoint],
        des_m: &[[f32; 128]],
        mm_sz: usize,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
        is_ug: bool,
        floor_id: Option<i32>,
    ) -> Option<(f64, f64, usize, f64, f64)> {
        let mut best: Option<(f64, f64, usize, f64, f64)> = None;
        for (dx, dy) in [(380.0, 0.0), (-380.0, 0.0), (0.0, 380.0), (0.0, -380.0)] {
            let nx = anchor_x + dx;
            let ny = anchor_y + dy;
            let Some(cell_n) =
                self.get_or_build_ref_cell(map_id, nx, ny, is_ug, floor_id, -1)
            else {
                continue;
            };
            if let Some(cand) = self.match_sift_feats(kp_m, des_m, mm_sz, cell_n, nx, ny, 300.0)
                && cand.2 >= 12
                && best.is_none_or(|b| cand.2 > b.2)
            {
                best = Some(cand);
            }
        }
        best
    }

    fn legacy_annulus_or_orb_match(
        &mut self,
        minimap_gray: ArrayView2<'_, u8>,
        map_id: i32,
        anchor_x: f64,
        anchor_y: f64,
    ) -> (Option<f64>, Option<f64>, f64, String, bool) {
        let cam_pinned_north = self
            .camera_heading
            .is_some_and(|ch| shortest_angle_delta(0.0, ch).abs() < self.config.follow_view_cam_pin_deg);
        let mut best_wx = None;
        let mut best_wy = None;
        let mut best_corr = -1.0f64;
        let mut match_method = "none".to_owned();
        let curr_regime = self.zoom_regime;
        let alt_regime = match curr_regime {
            ZoomRegime::OpenWorld => ZoomRegime::City,
            ZoomRegime::City => ZoomRegime::OpenWorld,
        };

        if self.minimap_mode == MinimapMode::FollowView {
            if let Some((o_wx, o_wy, theta_map, o_conf, _)) =
                self.estimate_follow_view_orb(minimap_gray, map_id, anchor_x, anchor_y, curr_regime)
            {
                self.map_rotation_deg = theta_map;
                self.view_heading = Some(wrap_angle_360(-self.map_rotation_deg));
                let derot = self.match_annulus_template(
                    minimap_gray,
                    map_id,
                    anchor_x,
                    anchor_y,
                    curr_regime,
                    -theta_map,
                    Some(0.0),
                );
                if let Some((dwx, dwy, dcorr)) = derot
                    && dcorr >= self.config.template_min_corr
                {
                    best_wx = Some(dwx);
                    best_wy = Some(dwy);
                    best_corr = dcorr;
                    match_method = "follow_view_annulus".to_owned();
                } else {
                    best_wx = Some(o_wx);
                    best_wy = Some(o_wy);
                    best_corr = o_conf;
                    match_method = "follow_view_orb".to_owned();
                }
            }
            return (best_wx, best_wy, best_corr, match_method, false);
        }

        let cone = self.cone_mask_heading();
        let curr_res = self.match_annulus_template(
            minimap_gray,
            map_id,
            anchor_x,
            anchor_y,
            curr_regime,
            0.0,
            cone,
        );
        let curr_corr = curr_res.map_or(-1.0, |r| r.2);

        let alt_res = if curr_corr <= (1.0 - self.config.zoom_switch_margin + 1e-6) {
            self.match_annulus_template(
                minimap_gray,
                map_id,
                anchor_x,
                anchor_y,
                alt_regime,
                0.0,
                cone,
            )
        } else {
            None
        };
        let alt_corr = alt_res.map_or(-1.0, |r| r.2);
        let alt_near_anchor =
            alt_res.is_some_and(|r| (r.0 - anchor_x).hypot(r.1 - anchor_y) <= 45.0);
        let alt_clear_win = alt_near_anchor
            && alt_corr >= self.config.zoom_switch_min_corr
            && (alt_corr - curr_corr.max(0.0)) >= self.config.zoom_switch_margin;

        let mut zoom_pending_switch = false;
        if alt_clear_win {
            self.zoom_alt_streak += 1;
            if self.zoom_alt_streak >= self.config.zoom_switch_consecutive_wins {
                self.zoom_regime = alt_regime;
                self.zoom_alt_streak = 0;
                let (awx, awy, acorr) = alt_res.unwrap();
                best_wx = Some(awx);
                best_wy = Some(awy);
                best_corr = acorr;
                match_method = "annulus_template_zoom_switch".to_owned();
            } else {
                zoom_pending_switch = true;
                if let Some((cwx, cwy, ccorr)) = curr_res
                    && ccorr >= self.config.template_min_corr
                {
                    best_wx = Some(cwx);
                    best_wy = Some(cwy);
                    best_corr = ccorr;
                    match_method = "annulus_template".to_owned();
                }
            }
        } else {
            self.zoom_alt_streak = 0;
            if let Some((cwx, cwy, ccorr)) = curr_res {
                best_wx = Some(cwx);
                best_wy = Some(cwy);
                best_corr = ccorr;
                match_method = "annulus_template".to_owned();
            }
        }

        if !zoom_pending_switch
            && (cam_pinned_north || self.camera_heading.is_none())
            && best_corr < 0.65
            && let Some((o_wx, o_wy, theta_map, o_conf, _)) =
                self.estimate_follow_view_orb(minimap_gray, map_id, anchor_x, anchor_y, self.zoom_regime)
        {
            let rot_mag = shortest_angle_delta(0.0, theta_map).abs();
            if rot_mag >= self.config.follow_view_min_map_rot_deg {
                self.minimap_mode = MinimapMode::FollowView;
                self.map_rotation_deg = theta_map;
                self.view_heading = Some(wrap_angle_360(-self.map_rotation_deg));
                let derot = self.match_annulus_template(
                    minimap_gray,
                    map_id,
                    anchor_x,
                    anchor_y,
                    self.zoom_regime,
                    -theta_map,
                    Some(0.0),
                );
                if let Some((dwx, dwy, dcorr)) = derot
                    && dcorr >= self.config.template_min_corr
                {
                    best_wx = Some(dwx);
                    best_wy = Some(dwy);
                    best_corr = dcorr;
                    match_method = "follow_view_annulus".to_owned();
                } else {
                    best_wx = Some(o_wx);
                    best_wy = Some(o_wy);
                    best_corr = o_conf;
                    match_method = "follow_view_orb".to_owned();
                }
            }
        }

        (best_wx, best_wy, best_corr, match_method, zoom_pending_switch)
    }

    #[allow(clippy::too_many_arguments)]
    fn try_global_teyvat_relocate(
        &mut self,
        mm_gray: ArrayView2<'_, u8>,
        mm_bgr: Option<ArrayView3<'_, u8>>,
        kp_m_in: Option<&[KeyPoint]>,
        des_m_in: Option<&[[f32; 128]]>,
        mm_sz_in: usize,
        ts: f64,
        p_score: f64,
        cone_excl: f64,
        t_match_start: Instant,
    ) -> Option<MinimapPose> {
        if !self.config.enable_global_relocate {
            return None;
        }
        let reloc = self.global_relocator.as_ref().map(Arc::clone)?;
        // CRITICAL INVARIANT: check is_ready() BEFORE incrementing global_query_count!
        if !reloc.is_ready() {
            return None;
        }

        let q_idx = self.global_query_count;
        self.global_query_count += 1;
        if !(q_idx < 6 || q_idx.is_multiple_of(4)) {
            return None;
        }

        let cands = if let Some(bgr) = mm_bgr {
            reloc.query_candidates(bgr, Some(&mut self.global_vote_bins), 3, 3.5)
        } else {
            reloc.query_candidates_gray(mm_gray, Some(&mut self.global_vote_bins), 3, 3.5)
        };
        if cands.is_empty() {
            return None;
        }

        let owned_sift;
        let (kp_m, des_m, mm_sz) = match (kp_m_in, des_m_in) {
            (Some(k), Some(d)) => (k, d, mm_sz_in),
            _ => {
                owned_sift = self.extract_mm_sift(mm_gray, mm_bgr, 128);
                (owned_sift.0.as_slice(), owned_sift.1.as_slice(), owned_sift.2)
            }
        };
        if kp_m.is_empty() || des_m.is_empty() {
            return None;
        }

        let mut best_global: Option<(f64, f64, usize, f64, f64)> = None;
        for cand in cands {
            let Some(cell_g) =
                self.get_or_build_ref_cell(2, cand.world_x, cand.world_y, false, None, -1)
            else {
                continue;
            };
            let Some((gwx, gwy, gninl, gspan, g_theta_map)) =
                self.match_sift_feats(kp_m, des_m, mm_sz, cell_g, cand.world_x, cand.world_y, 260.0)
            else {
                continue;
            };
            let mut verified = gninl >= 16;
            if !verified && gninl >= 10 {
                let is_fv = g_theta_map.abs() > 10.0;
                let s_chk = self.match_sobel_fast(
                    mm_gray,
                    cell_g,
                    gwx,
                    gwy,
                    gspan,
                    16.0,
                    if is_fv { -g_theta_map } else { 0.0 },
                    if is_fv {
                        Some(0.0)
                    } else {
                        self.cone_mask_heading()
                    },
                );
                if let Some((sx, sy, sc)) = s_chk
                    && sc >= 0.30
                    && (sx - gwx).hypot(sy - gwy) <= 12.0
                {
                    verified = true;
                }
            }
            if verified && best_global.is_none_or(|b| gninl > b.2) {
                best_global = Some((gwx, gwy, gninl, gspan, g_theta_map));
            }
        }

        let (gwx, gwy, gninl, gspan, g_theta_map) = best_global?;
        self.long_pause_pending = false;
        self.relocate_tier2_left = 0;
        self.just_resumed_from_pause = false;
        self.resume_frames_left = 0;
        self.motion_fallback_frames = 0;
        self.consecutive_sift_misses = 0;
        self.sift_interframe_budget = 0;
        self.pending_layer_key = None;
        self.pending_layer_score = 0;
        self.global_vote_bins.clear();
        self.global_query_count = 0;
        self.last_zoom_tier = -1;
        self.last_sift_inliers = gninl;
        self.update_zoom_from_span(gspan);
        self.apply_rotation_mode(g_theta_map, gninl);
        self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
        self.prev_cone_excl_for_phase = Some(cone_excl);
        self.prev_phase_gray = None;
        let _ = self.fsm.set_anchor(2, gwx, gwy, ts, false, None, "G");
        self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
        Some(self.build_pose(
            ts,
            false,
            "global_teyvat_relocate",
            Some("global_teyvat_relocate".to_owned()),
            true,
            p_score,
            gninl,
        ))
    }

    pub fn step(
        &mut self,
        minimap: Option<FrameView<'_>>,
        now: Option<f64>,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        self.step_with_paimon(minimap, None, now, 1.0, underground_overlay)
    }

    pub fn step_array3(
        &mut self,
        minimap: Option<ArrayView3<'_, u8>>,
        now: Option<f64>,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;
        match minimap {
            Some(arr) => {
                let gray = if arr.dim().2 == 1 {
                    arr.slice(s![.., .., 0]).to_owned()
                } else {
                    bgr_to_gray(arr)?
                };
                self.step_core(Some(gray.view()), Some(arr), 1.0, ts, underground_overlay)
            }
            None => self.step_core(None, None, 1.0, ts, underground_overlay),
        }
    }

    pub fn step_with_paimon(
        &mut self,
        minimap: Option<FrameView<'_>>,
        paimon_maybe: Option<FrameView<'_>>,
        now: Option<f64>,
        ui_scale: f64,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;

        let pm_gray_opt = paimon_maybe.and_then(|v| frame_view_to_gray_or_bgr_gray(v).ok());
        let p_score = self.check_paimon(pm_gray_opt.as_ref().map(|a| a.view()), ui_scale);
        if p_score < self.config.paimon_thresh {
            if p_score < 0.25
                && let Some(mm) = minimap
                && is_strided_blank_or_flash_frame(mm)
            {
                self.long_pause_pending = true;
                self.relocate_tier2_left = 2;
            }
            return self.pause(Some(ts), "paimon_hidden", p_score);
        }

        let (mm_gray_opt, mm_bgr_owned) = match minimap {
            Some(view) => frame_view_to_gray_and_bgr_opt(view),
            None => (None, None),
        };
        self.step_core(
            mm_gray_opt.as_ref().map(|g| g.view()),
            mm_bgr_owned.as_ref().map(|b| b.view()),
            p_score,
            ts,
            underground_overlay,
        )
    }

    pub fn step_from_hud(
        &mut self,
        hud_tl: Option<FrameView<'_>>,
        frame_w: u32,
        frame_h: u32,
        now: Option<f64>,
        gamepad_mode: bool,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;

        let Some(hud_view) = hud_tl else {
            return self.pause(Some(ts), "empty_hud", 0.0);
        };
        if hud_view.width == 0 || hud_view.height == 0 || hud_view.data.is_empty() {
            return self.pause(Some(ts), "empty_hud", 0.0);
        }

        let fsize = (frame_w, frame_h, gamepad_mode);
        if self.hud_frame_size != Some(fsize) {
            self.hud_frame_size = Some(fsize);
            self.cal_offset_samples.clear();
            self.cal_dx_px = 0;
            self.cal_dy_px = 0;
            self.cal_locked = false;
            self.paimon_last_xy_1080 = None;
        }

        let lay = self.get_layout(frame_w, frame_h, gamepad_mode)?;
        let hud_hw = hud_view.width as usize;
        let hud_hh = hud_view.height as usize;

        let pmx = lay.paimon_maybe_rect.x.max(0) as usize;
        let pmy = lay.paimon_maybe_rect.y.max(0) as usize;
        let pmw = lay.paimon_maybe_rect.width.max(0) as usize;
        let pmh = lay.paimon_maybe_rect.height.max(0) as usize;
        let pm_crop = crop_frame_view_gray(hud_view, pmx, pmy, pmw, pmh);
        let p_score = self.check_paimon(pm_crop.as_ref().map(|a| a.view()), lay.scale_ui);

        if self.has_paimon_tpl
            && p_score >= 0.72
            && !self.cal_locked
            && let Some((lx, ly)) = self.paimon_last_xy_1080
        {
            let px_phys = pmx as i32 + (lx as f64 * lay.scale_ui).round_ties_even() as i32;
            let py_phys = pmy as i32 + (ly as f64 * lay.scale_ui).round_ties_even() as i32;
            let raw_dx = px_phys - lay.paimon_rect.x;
            let raw_dy = py_phys - lay.paimon_rect.y;
            let lim_x = (45.0 * lay.scale_ui).round_ties_even() as i32;
            let lim_y = (25.0 * lay.scale_ui).round_ties_even() as i32;
            if raw_dx.abs() <= lim_x && raw_dy.abs() <= lim_y {
                self.cal_offset_samples.push((raw_dx, raw_dy));
                let dxs: Vec<i32> = self.cal_offset_samples.iter().map(|s| s.0).collect();
                let dys: Vec<i32> = self.cal_offset_samples.iter().map(|s| s.1).collect();
                self.cal_dx_px = median_i32_rounded(&dxs);
                self.cal_dy_px = median_i32_rounded(&dys);
                if self.cal_offset_samples.len() >= 3 {
                    self.cal_locked = true;
                }
            }
        }

        let mx = lay.minimap_rect.x;
        let my = lay.minimap_rect.y;
        let mw = lay.minimap_rect.width.max(0) as usize;
        let mh = lay.minimap_rect.height.max(0) as usize;
        let cx = (mx + self.cal_dx_px)
            .clamp(0, (hud_hw.saturating_sub(mw)) as i32) as usize;
        let cy = (my + self.cal_dy_px)
            .clamp(0, (hud_hh.saturating_sub(mh)) as i32) as usize;

        let (mm_gray_opt, mm_bgr_opt) = crop_frame_view_gray_and_bgr(hud_view, cx, cy, mw, mh);

        if self.has_paimon_tpl && p_score < self.config.paimon_thresh {
            if let Some(mg) = mm_gray_opt.as_ref()
                && is_strided_blank_or_flash_gray(mg.view())
            {
                self.long_pause_pending = true;
                self.relocate_tier2_left = 2;
            }
            return self.pause(Some(ts), "paimon_hidden", p_score);
        }

        let pose = self.step_core(
            mm_gray_opt.as_ref().map(|g| g.view()),
            mm_bgr_opt.as_ref().map(|b| b.view()),
            p_score,
            ts,
            underground_overlay,
        )?;

        if !self.has_paimon_tpl && self.paimon_cache_path.is_some() {
            if pose.state == TrackerState::Tracking && pose.sift_inliers >= 12 {
                self.paimon_bootstrap_streak += 1;
                if self.paimon_bootstrap_streak >= 3
                    && let Some(hud_gray) = crop_frame_view_gray(hud_view, 0, 0, hud_hw, hud_hh)
                {
                    self.try_bootstrap_paimon(hud_gray.view(), &lay);
                }
            } else {
                self.paimon_bootstrap_streak = 0;
            }
        }
        Ok(pose)
    }

    pub fn step_from_hud_array3(
        &mut self,
        hud_tl: Option<ArrayView3<'_, u8>>,
        frame_w: u32,
        frame_h: u32,
        now: Option<f64>,
        gamepad_mode: bool,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        let Some(arr) = hud_tl else {
            return self.step_from_hud(None, frame_w, frame_h, now, gamepad_mode, underground_overlay);
        };
        let (h, w, c) = arr.dim();
        let format = match c {
            1 => PixelFormat::Gray8,
            3 => PixelFormat::Bgr8,
            4 => PixelFormat::Bgra8,
            _ => return self.pause(now, "empty_hud", 0.0),
        };
        let owned;
        let data = if let Some(s) = arr.as_slice() {
            s
        } else {
            owned = arr.as_standard_layout().into_owned();
            owned.as_slice().unwrap()
        };
        let fv = FrameView {
            width: w as u32,
            height: h as u32,
            stride: w * c,
            format,
            data,
        };
        self.step_from_hud(Some(fv), frame_w, frame_h, now, gamepad_mode, underground_overlay)
    }

    pub fn step_headings_only(
        &mut self,
        minimap: Option<FrameView<'_>>,
        now: Option<f64>,
    ) -> Option<MinimapPose> {
        let view = minimap?;
        if view.width == 0 || view.height == 0 || view.format == PixelFormat::Gray8 {
            return None;
        }
        if self.fsm.state != TrackerState::Tracking
            || self.pause_start_ts.is_some()
            || self.long_pause_pending
        {
            return None;
        }
        let arr = frame_view_to_bgr_preserving_alpha(view)?;
        self.step_headings_only_array3(Some(arr.view()), now)
    }

    pub fn step_headings_only_array3(
        &mut self,
        minimap: Option<ArrayView3<'_, u8>>,
        now: Option<f64>,
    ) -> Option<MinimapPose> {
        let arr = minimap?;
        let (h, w, c) = arr.dim();
        if h == 0 || w == 0 || (c != 3 && c != 4) {
            return None;
        }
        if self.fsm.state != TrackerState::Tracking
            || self.pause_start_ts.is_some()
            || self.long_pause_pending
        {
            return None;
        }
        let mut samples = Vec::with_capacity(((h / 6) + 1) * ((w / 6) + 1) * 3);
        for y in (0..h).step_by(6) {
            for x in (0..w).step_by(6) {
                samples.push(arr[[y, x, 0]]);
                samples.push(arr[[y, x, 1]]);
                samples.push(arr[[y, x, 2]]);
            }
        }
        let mean = mean_f64_u8(&samples);
        let std = std_f64_u8(&samples);
        if std < 8.0 || mean < 20.0 || mean > 240.0 {
            return None;
        }
        let ts = self.resolve_timestamp(now);
        self.last_timestamp = ts;
        self.update_headings_from_color(Some(arr), f64::INFINITY, f64::INFINITY, f64::INFINITY, ts);
        Some(self.build_pose(ts, true, "heading_fast", None, false, 1.0, 0))
    }

    fn step_core(
        &mut self,
        mm_gray_opt: Option<ArrayView2<'_, u8>>,
        mm_bgr_opt: Option<ArrayView3<'_, u8>>,
        p_score: f64,
        ts: f64,
        underground_overlay: Option<UndergroundOverlayInput<'_>>,
    ) -> CvResult<MinimapPose> {
        self.last_match_ms = 0.0;
        if let Some(ps) = self.pause_start_ts {
            if (ts - ps) >= 1.2 {
                self.long_pause_pending = true;
                self.relocate_tier2_left = 2;
            }
            self.pause_start_ts = None;
        }

        // 1. Fast Gate + Cyan Arrow Rescue
        let mut gate_res = if let Some(bgr) = mm_bgr_opt {
            self.gate.check_array3(Some(bgr), false)
        } else {
            self.gate.check_gray(mm_gray_opt, false)
        };
        self.last_hdr_clipped = gate_res.hdr_clipped;
        if !gate_res.valid
            && gate_res.reason.as_deref() == Some("no_circular_minimap_border")
            && gate_res.inner_std >= self.config.gate_min_std
            && let Some(bgr) = mm_bgr_opt
        {
            let rescue = extract_player_arrow_360(bgr);
            if rescue.heading_deg.is_some() && rescue.confidence >= 0.55 {
                gate_res.valid = true;
                gate_res.reason = None;
            }
        }

        if !gate_res.valid {
            self.prev_phase_gray = None;
            self.prev_mm_gray_for_phase = None;
            self.sift_interframe_budget = 0;
            self.zoom_alt_streak = 0;
            if self.config.hold_on_phase_exhausted
                && self.fsm.state == TrackerState::Tracking
                && self.fsm.x.is_some()
            {
                self.fsm.touch_stationary(ts)?;
                return Ok(self.build_pose(
                    ts,
                    true,
                    "gate_hold",
                    gate_res.reason,
                    false,
                    p_score,
                    0,
                ));
            }
            self.motion_fallback_frames = 0;
            let (_, _, reason) = self.fsm.update(
                &PoseMeasurement {
                    found: false,
                    reason: gate_res.reason,
                    ..PoseMeasurement::default()
                },
                ts,
            )?;
            return Ok(self.build_pose(
                ts,
                false,
                "gate_rejected",
                reason,
                false,
                p_score,
                0,
            ));
        }

        let mm_gray = mm_gray_opt.expect("valid gate implies non-empty minimap");

        // 2. Extract 360 headings
        self.update_headings_from_color(
            mm_bgr_opt,
            gate_res.frame_diff,
            gate_res.alpha_diff,
            gate_res.center_diff,
            ts,
        );

        // 3. Stationary fast-path skip
        let mut terrain_stationary = gate_res.stationary;
        if !terrain_stationary
            && !self.long_pause_pending
            && self.fsm.state == TrackerState::Tracking
            && self.fsm.x.is_some()
            && self.minimap_mode == MinimapMode::LockDirection
            && let Some(cone_h) = self.cone_mask_heading()
        {
            terrain_stationary =
                self.gate
                    .is_terrain_stationary_excluding_cones(Some(cone_h), 56.0, None);
        }

        if terrain_stationary
            && !self.long_pause_pending
            && self.fsm.state == TrackerState::Tracking
            && self.fsm.x.is_some()
        {
            if gate_res.center_diff >= self.config.gate_cone_change_diff
                || gate_res.alpha_diff >= self.config.gate_cone_change_diff
            {
                if let Some(bgr) = mm_bgr_opt {
                    self.gate.commit_heading_keyframe_array3(bgr);
                } else {
                    self.gate.commit_heading_keyframe_gray(mm_gray);
                }
            }
            self.stationary_skips += 1;
            self.fsm.touch_stationary(ts)?;
            return Ok(self.build_pose(
                ts,
                true,
                "stationary_skip",
                None,
                false,
                p_score,
                0,
            ));
        }

        let cam_for_kf = self.cone_mask_heading();
        if let Some(bgr) = mm_bgr_opt {
            self.gate.commit_keyframe_array3(bgr, cam_for_kf);
        } else {
            self.gate.commit_keyframe_gray(mm_gray, cam_for_kf);
        }

        let cone_excl = if self.minimap_mode == MinimapMode::LockDirection {
            self.cone_mask_heading().unwrap_or(0.0)
        } else {
            0.0
        };

        // Cold-start anchor search
        if self.fsm.x.is_none() || self.fsm.y.is_none() || self.fsm.map_id.is_none() {
            if let (Some(loc), Some(bgr)) = (self.global_locator.as_ref(), mm_bgr_opt)
                && let Some((g_mid, g_wx, g_wy, g_conf)) = loc(bgr, self.fsm.map_id)
            {
                let meas = PoseMeasurement {
                    found: true,
                    map_id: Some(g_mid),
                    world_x: Some(g_wx),
                    world_y: Some(g_wy),
                    confidence: g_conf,
                    is_underground: self.fsm.is_underground,
                    floor_id: self.fsm.floor_id,
                    floor_name: self.fsm.floor_name.clone(),
                    match_method: "global".to_owned(),
                    reason: None,
                };
                let (acc, _, reason) = self.fsm.update(&meas, ts)?;
                self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
                self.prev_cone_excl_for_phase = Some(cone_excl);
                self.prev_phase_gray = None;
                return Ok(self.build_pose(ts, false, "global", reason, acc, p_score, 0));
            }
            if matches!(self.fsm.map_id, None | Some(2)) {
                let mm_sz = mm_gray.dim().0.min(mm_gray.dim().1);
                if let Some(cold_pose) = self.try_global_teyvat_relocate(
                    mm_gray,
                    mm_bgr_opt,
                    None,
                    None,
                    mm_sz,
                    ts,
                    p_score,
                    cone_excl,
                    Instant::now(),
                ) {
                    self.matcher_calls += 1;
                    return Ok(cold_pose);
                }
            }
            self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
            self.prev_cone_excl_for_phase = Some(cone_excl);
            self.prev_phase_gray = None;
            let (_, _, reason) = self.fsm.update(
                &PoseMeasurement {
                    found: false,
                    reason: Some("no_anchor".to_owned()),
                    ..PoseMeasurement::default()
                },
                ts,
            )?;
            return Ok(self.build_pose(ts, false, "none", reason, false, p_score, 0));
        }

        let t_match_start = Instant::now();
        self.matcher_calls += 1;
        let map_id = self.fsm.map_id.unwrap();
        let anchor_x = self.fsm.x.unwrap();
        let anchor_y = self.fsm.y.unwrap();
        let is_ug = self.fsm.is_underground;
        let floor_id = self.fsm.floor_id;

        // Underground without overlay provider: bounded odometry hold
        if is_ug && self.ug_provider.is_none() && underground_overlay.is_none() {
            let curr_phase = self.prepare_phase_patch(mm_gray, Some(cone_excl));
            let phase_res = curr_phase
                .as_ref()
                .and_then(|cp| self.run_phase_correlate_fallback(cp.view()));
            self.prev_phase_gray = curr_phase;
            if let Some((p_wx, p_wy, p_conf)) = phase_res {
                let step_d = (p_wx - anchor_x).hypot(p_wy - anchor_y);
                if step_d <= 18.0 {
                    let meas = PoseMeasurement {
                        found: true,
                        map_id: Some(map_id),
                        world_x: Some(p_wx),
                        world_y: Some(p_wy),
                        confidence: p_conf,
                        is_underground: true,
                        floor_id,
                        floor_name: self.fsm.floor_name.clone(),
                        match_method: "motion".to_owned(),
                        reason: None,
                    };
                    let (_, _, reason) = self.fsm.update(&meas, ts)?;
                    self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                    return Ok(self.build_pose(
                        ts,
                        false,
                        "underground_odometry",
                        reason,
                        false,
                        p_score,
                        0,
                    ));
                }
            }
            self.fsm.touch_stationary(ts)?;
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(self.build_pose(
                ts,
                true,
                "underground_hold",
                None,
                false,
                p_score,
                0,
            ));
        }

        self.layer_probe_tick += 1;

        // 4B. Hybrid Inter-Frame Zero-Mean Sobel Fast Path (skipped while Paimon bootstrap is pending!)
        if self.config.enable_interframe_sobel
            && self.sift_interframe_budget > 0
            && (self.has_paimon_tpl || self.paimon_cache_path.is_none())
            && self.span_confirmed
            && self.minimap_mode == MinimapMode::LockDirection
            && !self.just_resumed_from_pause
            && !self.long_pause_pending
            && self.consecutive_sift_misses == 0
            && self.pending_layer_score == 0
        {
            let want_z0_fast =
                self.config.enable_z0_city && !is_ug && self.confirmed_span_world < 195.0;
            let mut z_tier_fast = if want_z0_fast { 0 } else { -1 };
            let mut cell_fast = self.get_or_build_ref_cell(
                map_id,
                anchor_x,
                anchor_y,
                is_ug,
                floor_id,
                z_tier_fast,
            );
            if cell_fast.is_none() && z_tier_fast == 0 {
                z_tier_fast = -1;
                cell_fast =
                    self.get_or_build_ref_cell(map_id, anchor_x, anchor_y, is_ug, floor_id, -1);
            }
            self.sift_interframe_budget -= 1;
            if let Some(cf_idx) = cell_fast {
                let fast_res = self.match_sobel_fast(
                    mm_gray,
                    cf_idx,
                    anchor_x,
                    anchor_y,
                    self.confirmed_span_world,
                    18.0,
                    0.0,
                    Some(cone_excl),
                );
                if let Some((wx, wy, sc)) = fast_res
                    && sc >= self.config.interframe_sobel_min_corr
                    && (wx - anchor_x).hypot(wy - anchor_y) <= 24.0
                {
                    self.last_zoom_tier = z_tier_fast;
                    self.motion_fallback_frames = 0;
                    self.view_heading = self.camera_heading;
                    let meas = PoseMeasurement {
                        found: true,
                        map_id: Some(map_id),
                        world_x: Some(wx),
                        world_y: Some(wy),
                        confidence: sc.clamp(0.0, 1.0),
                        is_underground: is_ug,
                        floor_id,
                        floor_name: self.fsm.floor_name.clone(),
                        match_method: "hd_sobel_fast".to_owned(),
                        reason: None,
                    };
                    let (acc, _, reason) = self.fsm.update(&meas, ts)?;
                    self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
                    self.prev_cone_excl_for_phase = Some(cone_excl);
                    self.prev_phase_gray = None;
                    self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                    return Ok(self.build_pose(
                        ts,
                        false,
                        "hd_sobel_fast",
                        reason,
                        acc,
                        p_score,
                        0,
                    ));
                }
            }
            self.sift_interframe_budget = 0;
        }

        // 5. Primary: Single-pass Local-Cell SIFT (128x128)
        let (kp_m, des_m, mm_sz) = self.extract_mm_sift(mm_gray, mm_bgr_opt, 128);
        let wide_search = self.just_resumed_from_pause || self.consecutive_sift_misses >= 2;
        let max_dist = if wide_search { 320.0 } else { 85.0 };
        let want_z0 = self.config.enable_z0_city
            && !is_ug
            && !wide_search
            && !self.long_pause_pending
            && self.span_confirmed
            && self.confirmed_span_world < 195.0;
        let mut z_tier = if want_z0 { 0 } else { -1 };
        let mut cell_opt =
            self.get_or_build_ref_cell(map_id, anchor_x, anchor_y, is_ug, floor_id, z_tier);
        if cell_opt.is_none() && z_tier == 0 {
            z_tier = -1;
            cell_opt = self.get_or_build_ref_cell(map_id, anchor_x, anchor_y, is_ug, floor_id, -1);
        }

        let mut sift_res = None;
        if let Some(c_idx) = cell_opt
            && let Some(cand) =
                self.match_sift_feats(&kp_m, &des_m, mm_sz, c_idx, anchor_x, anchor_y, max_dist)
        {
            let c_dist = (cand.0 - anchor_x).hypot(cand.1 - anchor_y);
            if c_dist <= 85.0 || (self.just_resumed_from_pause && cand.2 >= 7) || cand.2 >= 9 {
                sift_res = Some(cand);
            }
        }

        if sift_res.is_none() && z_tier == 0 {
            z_tier = -1;
            if let Some(cell_n1) =
                self.get_or_build_ref_cell(map_id, anchor_x, anchor_y, is_ug, floor_id, -1)
            {
                cell_opt = Some(cell_n1);
                if let Some(cand) =
                    self.match_sift_feats(&kp_m, &des_m, mm_sz, cell_n1, anchor_x, anchor_y, max_dist)
                {
                    let c_dist = (cand.0 - anchor_x).hypot(cand.1 - anchor_y);
                    if c_dist <= 85.0 || cand.2 >= 9 {
                        sift_res = Some(cand);
                    }
                }
            }
        }

        self.last_zoom_tier = z_tier;

        // 5B. Automatic Surface <-> Underground floor switch probe
        let cur_ninl = sift_res.map_or(0, |r| r.2);
        self.last_sift_inliers = cur_ninl;
        if sift_res.is_some() && cur_ninl >= 10 {
            self.pending_layer_key = None;
            self.pending_layer_score = 0;
        } else if self.ug_provider.is_some()
            && self.config.enable_auto_layer
            && ((sift_res.is_none()
                && (self.just_resumed_from_pause
                    || self.consecutive_sift_misses >= 1
                    || self.pending_layer_score > 0))
                || (self.layer_probe_tick.is_multiple_of(12) && cur_ninl < 10))
            && let Some(layer_pose) = self.probe_candidate_layers(
                &kp_m,
                &des_m,
                mm_sz,
                mm_gray,
                cone_excl,
                map_id,
                anchor_x,
                anchor_y,
                is_ug,
                floor_id,
                cur_ninl,
                max_dist,
                self.just_resumed_from_pause,
                ts,
                p_score,
            )
        {
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(layer_pose);
        }

        // 5C. Tier-2 Post-Loading Neighbor Probing (+-380w)
        let mut tier2_reacquire = false;
        if sift_res.is_none() && self.long_pause_pending && self.relocate_tier2_left > 0 {
            self.relocate_tier2_left -= 1;
            if let Some(cand_t2) = self.probe_tier2_neighbors(
                &kp_m, &des_m, mm_sz, map_id, anchor_x, anchor_y, is_ug, floor_id,
            ) {
                sift_res = Some(cand_t2);
                tier2_reacquire = true;
            }
        }

        if let Some((wx, wy, ninl, span_w, theta_map)) = sift_res {
            let dist_from_anchor = (wx - anchor_x).hypot(wy - anchor_y);
            let was_resumed = self.just_resumed_from_pause;
            self.motion_fallback_frames = 0;
            self.consecutive_sift_misses = 0;
            self.just_resumed_from_pause = false;
            self.resume_frames_left = 0;
            self.long_pause_pending = false;
            self.relocate_tier2_left = 0;
            self.global_vote_bins.clear();
            self.global_query_count = 0;

            self.update_zoom_from_span(span_w);
            self.apply_rotation_mode(theta_map, ninl);
            self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
            self.prev_cone_excl_for_phase = Some(cone_excl);
            self.prev_phase_gray = None;

            if (was_resumed && dist_from_anchor > 55.0 && ninl >= 7) || tier2_reacquire {
                self.sift_interframe_budget = 0;
                let fname = self.fsm.floor_name.clone();
                self.fsm
                    .set_anchor(map_id, wx, wy, ts, is_ug, floor_id, &fname)?;
                self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                return Ok(self.build_pose(
                    ts,
                    false,
                    if tier2_reacquire {
                        "hd_sift_tier2"
                    } else {
                        "hd_sift_reacquire"
                    },
                    Some(
                        if tier2_reacquire {
                            "tier2_reacquire"
                        } else {
                            "post_pause_reacquire"
                        }
                        .to_owned(),
                    ),
                    true,
                    p_score,
                    ninl,
                ));
            }

            let conf = (0.45 + 0.04 * ninl as f64).min(0.98);
            let meas = PoseMeasurement {
                found: true,
                map_id: Some(map_id),
                world_x: Some(wx),
                world_y: Some(wy),
                confidence: conf,
                is_underground: is_ug,
                floor_id,
                floor_name: self.fsm.floor_name.clone(),
                match_method: "hd_sift".to_owned(),
                reason: None,
            };
            let (acc, _, reason) = self.fsm.update(&meas, ts)?;
            let should_bc = acc && ninl >= self.config.broadcast_min_sift_inliers;
            if acc
                && ninl >= 10
                && self.minimap_mode == MinimapMode::LockDirection
                && self.span_confirmed
                && self.config.enable_interframe_sobel
            {
                self.sift_interframe_budget = self.config.interframe_sobel_k;
            } else {
                self.sift_interframe_budget = 0;
            }
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(self.build_pose(
                ts,
                false,
                "hd_sift",
                reason,
                should_bc,
                p_score,
                ninl,
            ));
        }

        self.sift_interframe_budget = 0;
        self.consecutive_sift_misses += 1;
        if self.resume_frames_left > 0 {
            self.resume_frames_left -= 1;
            if self.resume_frames_left == 0 {
                self.just_resumed_from_pause = false;
            }
        }

        // 5D. Safe Relocating Guard + Tier-3 Full-Teyvat Global Auto-Relocate
        if self.config.enable_relocating_guard && self.long_pause_pending {
            let strict_sobel = if let Some(c_idx) = cell_opt
                && self.span_confirmed
            {
                let is_fv = self.minimap_mode == MinimapMode::FollowView;
                self.match_sobel_fast(
                    mm_gray,
                    c_idx,
                    anchor_x,
                    anchor_y,
                    self.confirmed_span_world,
                    24.0,
                    if is_fv { -self.map_rotation_deg } else { 0.0 },
                    if is_fv {
                        Some(0.0)
                    } else {
                        self.cone_mask_heading()
                    },
                )
            } else {
                None
            };

            if let Some((wx, wy, sc)) = strict_sobel
                && sc >= 0.58
            {
                self.long_pause_pending = false;
                self.relocate_tier2_left = 0;
                self.just_resumed_from_pause = false;
                self.motion_fallback_frames = 0;
                self.global_vote_bins.clear();
                self.global_query_count = 0;
                let meas = PoseMeasurement {
                    found: true,
                    map_id: Some(map_id),
                    world_x: Some(wx),
                    world_y: Some(wy),
                    confidence: sc.min(1.0),
                    is_underground: is_ug,
                    floor_id,
                    floor_name: self.fsm.floor_name.clone(),
                    match_method: "hd_sobel_resume".to_owned(),
                    reason: None,
                };
                let (acc, _, reason) = self.fsm.update(&meas, ts)?;
                self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
                self.prev_cone_excl_for_phase = Some(cone_excl);
                self.prev_phase_gray = None;
                self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                return Ok(self.build_pose(ts, false, "hd_sobel", reason, acc, p_score, 0));
            }

            if map_id == 2
                && let Some(glob_pose) = self.try_global_teyvat_relocate(
                    mm_gray,
                    mm_bgr_opt,
                    Some(&kp_m),
                    Some(&des_m),
                    mm_sz,
                    ts,
                    p_score,
                    cone_excl,
                    t_match_start,
                )
            {
                return Ok(glob_pose);
            }

            self.prev_phase_gray = None;
            self.prev_mm_gray_for_phase = None;
            self.fsm.touch_stationary(ts)?;
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(self.build_pose(
                ts,
                false,
                "relocating_hold",
                Some("teleport_relocating_hold".to_owned()),
                false,
                p_score,
                0,
            ));
        }

        // 6. Secondary Fallback: Fast Zero-Mean Sobel
        let mut sobel_res: Option<(f64, f64, f64)> = None;
        if let Some(c_idx) = cell_opt {
            let probe_spans: &[f64] = if self.span_confirmed {
                &[self.confirmed_span_world]
            } else {
                &[180.0, 360.0]
            };
            let is_fv = self.minimap_mode == MinimapMode::FollowView;
            let pre_rot = if is_fv { -self.map_rotation_deg } else { 0.0 };
            let cone_h = if is_fv {
                Some(0.0)
            } else {
                self.cone_mask_heading()
            };
            for &p_span in probe_spans {
                if let Some(cand_s) = self.match_sobel_fast(
                    mm_gray, c_idx, anchor_x, anchor_y, p_span, 42.0, pre_rot, cone_h,
                ) && sobel_res.is_none_or(|b| cand_s.2 > b.2)
                {
                    sobel_res = Some(cand_s);
                    if !self.span_confirmed && cand_s.2 >= self.config.sobel_min_corr {
                        self.confirmed_span_world = p_span;
                    }
                }
            }
        }
        if let Some((wx, wy, sc)) = sobel_res
            && sc >= self.config.sobel_min_corr
        {
            self.motion_fallback_frames = 0;
            self.view_heading = if self.minimap_mode == MinimapMode::FollowView {
                Some(wrap_angle_360(-self.map_rotation_deg))
            } else {
                self.camera_heading
            };
            let meas = PoseMeasurement {
                found: true,
                map_id: Some(map_id),
                world_x: Some(wx),
                world_y: Some(wy),
                confidence: sc.clamp(0.0, 1.0),
                is_underground: is_ug,
                floor_id,
                floor_name: self.fsm.floor_name.clone(),
                match_method: "hd_sobel".to_owned(),
                reason: None,
            };
            let (acc, _, reason) = self.fsm.update(&meas, ts)?;
            self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
            self.prev_cone_excl_for_phase = Some(cone_excl);
            self.prev_phase_gray = None;
            let should_bc = acc && sc >= self.config.broadcast_min_sobel_corr;
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(self.build_pose(
                ts,
                false,
                "hd_sobel",
                reason,
                should_bc,
                p_score,
                0,
            ));
        }

        // 6B. Legacy z=-2 Annulus / ORB fallback
        if !is_ug {
            let (l_wx_opt, l_wy_opt, l_corr, l_method, zoom_pending) =
                self.legacy_annulus_or_orb_match(mm_gray, map_id, anchor_x, anchor_y);
            if let (Some(l_wx), Some(l_wy)) = (l_wx_opt, l_wy_opt)
                && l_corr >= self.config.template_min_corr
                && (l_corr >= 0.65 || (l_wx - anchor_x).hypot(l_wy - anchor_y) <= 45.0)
            {
                self.motion_fallback_frames = 0;
                let meas = PoseMeasurement {
                    found: true,
                    map_id: Some(map_id),
                    world_x: Some(l_wx),
                    world_y: Some(l_wy),
                    confidence: l_corr.clamp(0.0, 1.0),
                    is_underground: is_ug,
                    floor_id,
                    floor_name: self.fsm.floor_name.clone(),
                    match_method: l_method.clone(),
                    reason: None,
                };
                let (acc, _, reason) = self.fsm.update(&meas, ts)?;
                self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
                self.prev_cone_excl_for_phase = Some(cone_excl);
                self.prev_phase_gray = None;
                self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                return Ok(self.build_pose(ts, false, &l_method, reason, acc, p_score, 0));
            }
            if zoom_pending && l_corr < self.config.template_min_corr {
                self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
                self.prev_cone_excl_for_phase = Some(cone_excl);
                self.prev_phase_gray = None;
                let (_, _, reason) = self.fsm.update(
                    &PoseMeasurement {
                        found: false,
                        reason: Some("zoom_switch_pending".to_owned()),
                        ..PoseMeasurement::default()
                    },
                    ts,
                )?;
                self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
                return Ok(self.build_pose(
                    ts,
                    false,
                    "zoom_switch_pending",
                    reason,
                    false,
                    p_score,
                    0,
                ));
            }
        }

        // 7. Bounded phaseCorrelate fallback
        if self.pending_layer_score > 0 && cur_ninl == 0 {
            self.prev_phase_gray = None;
            self.prev_mm_gray_for_phase = None;
            self.fsm.touch_stationary(ts)?;
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            return Ok(self.build_pose(
                ts,
                false,
                "anchor_hold",
                Some("pending_layer_hold".to_owned()),
                false,
                p_score,
                0,
            ));
        }

        if self.prev_phase_gray.is_none()
            && let Some(prev_mm) = self.prev_mm_gray_for_phase.take()
        {
            self.prev_phase_gray =
                self.prepare_phase_patch(prev_mm.view(), self.prev_cone_excl_for_phase);
        }
        let curr_phase = self.prepare_phase_patch(mm_gray, Some(cone_excl));
        self.prev_mm_gray_for_phase = Some(mm_gray.to_owned());
        self.prev_cone_excl_for_phase = Some(cone_excl);

        if self.motion_fallback_frames >= self.config.max_phase_fallback_frames {
            self.prev_phase_gray = curr_phase;
            self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
            if self.config.hold_on_phase_exhausted {
                self.fsm.touch_stationary(ts)?;
                return Ok(self.build_pose(
                    ts,
                    false,
                    "anchor_hold",
                    Some("low_texture_hold".to_owned()),
                    false,
                    p_score,
                    0,
                ));
            }
            self.fsm.force_lost(ts)?;
            return Ok(self.build_pose(
                ts,
                false,
                "phase_budget_exhausted",
                Some("motion_fallback_budget_exceeded".to_owned()),
                false,
                p_score,
                0,
            ));
        }

        let phase_res = curr_phase
            .as_ref()
            .and_then(|cp| self.run_phase_correlate_fallback(cp.view()));
        self.prev_phase_gray = curr_phase;

        let match_method: String;
        let reason: Option<String>;
        if let Some((p_wx, p_wy, p_conf)) = phase_res {
            self.motion_fallback_frames += 1;
            match_method = "motion".to_owned();
            let meas = PoseMeasurement {
                found: true,
                map_id: Some(map_id),
                world_x: Some(p_wx),
                world_y: Some(p_wy),
                confidence: p_conf,
                is_underground: is_ug,
                floor_id,
                floor_name: self.fsm.floor_name.clone(),
                match_method: "motion".to_owned(),
                reason: None,
            };
            (_, _, reason) = self.fsm.update(&meas, ts)?;
        } else if self.config.hold_on_phase_exhausted
            && self.fsm.state == TrackerState::Tracking
            && self.fsm.x.is_some()
        {
            self.fsm.touch_stationary(ts)?;
            match_method = "anchor_hold".to_owned();
            reason = Some("low_texture_hold".to_owned());
        } else {
            match_method = "none".to_owned();
            (_, _, reason) = self.fsm.update(
                &PoseMeasurement {
                    found: false,
                    reason: Some("template_and_phase_failed".to_owned()),
                    ..PoseMeasurement::default()
                },
                ts,
            )?;
        }

        // 8. Optional explicit multi-layer floor comparison
        if let Some(ug_in) = underground_overlay
            && self.fsm.state == TrackerState::Tracking
            && let (Some(fx), Some(fy), Some(fmid)) = (self.fsm.x, self.fsm.y, self.fsm.map_id)
        {
            let span_w = if self.span_confirmed {
                self.confirmed_span_world
            } else {
                self.span_for_regime(self.zoom_regime)
            };
            let half_e = 0.5 * span_w;
            let cmp_ppu = 0.5;
            let (surf_p, _) = self.tile_cache.extract_local_patch(fmid, fx, fy, half_e, cmp_ppu);
            let (ug_p, _) = self.tile_cache.extract_underground_patch(
                Some(ug_in.image),
                &ug_in.bounds,
                fx,
                fy,
                half_e,
                cmp_ppu,
                None,
            );
            if let (Some(sp), Some(up)) = (surf_p, ug_p) {
                self.compare_surface_vs_underground_gray(
                    mm_gray,
                    Some(sp.view()),
                    Some(up.view()),
                    ug_in.floor_id,
                    ug_in.floor_name,
                    Some(cmp_ppu),
                    true,
                );
            }
        }

        self.last_match_ms = t_match_start.elapsed().as_secs_f64() * 1000.0;
        Ok(self.build_pose(ts, false, &match_method, reason, false, p_score, 0))
    }
}

fn frame_view_to_gray_or_bgr_gray(view: FrameView<'_>) -> CvResult<Array2<u8>> {
    crate::ops::color::frame_view_to_gray_array(view)
}

fn frame_view_to_bgr_preserving_alpha(view: FrameView<'_>) -> Option<Array3<u8>> {
    match view.format {
        PixelFormat::Gray8 => None,
        PixelFormat::Bgr8 | PixelFormat::Bgra8 => frame_view_to_array3(view).ok(),
        PixelFormat::Rgba8 => {
            let arr = frame_view_to_array3(view).ok()?;
            crate::ops::color::swap_rb(arr.view()).ok()
        }
    }
}

fn frame_view_to_gray_and_bgr_opt(view: FrameView<'_>) -> (Option<Array2<u8>>, Option<Array3<u8>>) {
    if view.width == 0 || view.height == 0 || view.data.is_empty() {
        return (None, None);
    }
    match view.format {
        PixelFormat::Gray8 => (frame_view_to_gray_or_bgr_gray(view).ok(), None),
        PixelFormat::Bgr8 | PixelFormat::Bgra8 | PixelFormat::Rgba8 => {
            let Some(bgr) = frame_view_to_bgr_preserving_alpha(view) else {
                return (None, None);
            };
            let gray = bgr_to_gray(bgr.view()).ok();
            (gray, Some(bgr))
        }
    }
}

fn crop_frame_view_gray(
    view: FrameView<'_>,
    x0: usize,
    y0: usize,
    w: usize,
    h: usize,
) -> Option<Array2<u8>> {
    let vw = view.width as usize;
    let vh = view.height as usize;
    let x1 = vw.min(x0 + w);
    let y1 = vh.min(y0 + h);
    if x1 <= x0 || y1 <= y0 {
        return None;
    }
    let ch = view.format.channels();
    if view.stride < vw * ch || view.data.len() < (vh - 1) * view.stride + vw * ch {
        return None;
    }
    let cw = x1 - x0;
    let ch_h = y1 - y0;
    let mut out = Array2::<u8>::zeros((ch_h, cw));
    for dy in 0..ch_h {
        let row = &view.data[(y0 + dy) * view.stride..];
        match view.format {
            PixelFormat::Gray8 => {
                out.row_mut(dy)
                    .as_slice_mut()
                    .unwrap()
                    .copy_from_slice(&row[x0..x1]);
            }
            PixelFormat::Bgr8 | PixelFormat::Bgra8 => {
                for dx in 0..cw {
                    let off = (x0 + dx) * ch;
                    out[[dy, dx]] = bgr_px_to_gray(row[off], row[off + 1], row[off + 2]);
                }
            }
            PixelFormat::Rgba8 => {
                for dx in 0..cw {
                    let off = (x0 + dx) * 4;
                    out[[dy, dx]] = bgr_px_to_gray(row[off + 2], row[off + 1], row[off]);
                }
            }
        }
    }
    Some(out)
}

fn crop_frame_view_gray_and_bgr(
    view: FrameView<'_>,
    x0: usize,
    y0: usize,
    w: usize,
    h: usize,
) -> (Option<Array2<u8>>, Option<Array3<u8>>) {
    let vw = view.width as usize;
    let vh = view.height as usize;
    let x1 = vw.min(x0 + w);
    let y1 = vh.min(y0 + h);
    if x1 <= x0 || y1 <= y0 {
        return (None, None);
    }
    let ch = view.format.channels();
    if view.stride < vw * ch || view.data.len() < (vh - 1) * view.stride + vw * ch {
        return (None, None);
    }
    if view.format == PixelFormat::Gray8 {
        return (crop_frame_view_gray(view, x0, y0, w, h), None);
    }
    let cw = x1 - x0;
    let ch_h = y1 - y0;
    let out_c = if ch == 4 { 4 } else { 3 };
    let mut bgr = Array3::<u8>::zeros((ch_h, cw, out_c));
    let mut gray = Array2::<u8>::zeros((ch_h, cw));
    for dy in 0..ch_h {
        let row = &view.data[(y0 + dy) * view.stride..];
        for dx in 0..cw {
            let off = (x0 + dx) * ch;
            let (b, g, r, a) = match view.format {
                PixelFormat::Bgr8 => (row[off], row[off + 1], row[off + 2], 255),
                PixelFormat::Bgra8 => (row[off], row[off + 1], row[off + 2], row[off + 3]),
                PixelFormat::Rgba8 => (row[off + 2], row[off + 1], row[off], row[off + 3]),
                PixelFormat::Gray8 => unreachable!(),
            };
            bgr[[dy, dx, 0]] = b;
            bgr[[dy, dx, 1]] = g;
            bgr[[dy, dx, 2]] = r;
            if out_c == 4 {
                bgr[[dy, dx, 3]] = a;
            }
            gray[[dy, dx]] = bgr_px_to_gray(b, g, r);
        }
    }
    (Some(gray), Some(bgr))
}

fn is_strided_blank_or_flash_gray(gray: ArrayView2<'_, u8>) -> bool {
    let (h, w) = gray.dim();
    let mut vals = Vec::with_capacity(((h / 4) + 1) * ((w / 4) + 1));
    for y in (0..h).step_by(4) {
        for x in (0..w).step_by(4) {
            vals.push(gray[[y, x]]);
        }
    }
    let std = std_f64_u8(&vals);
    let mean = mean_f64_u8(&vals);
    std < 12.0 || mean < 22.0 || mean > 238.0
}

fn is_strided_blank_or_flash_frame(view: FrameView<'_>) -> bool {
    let w = view.width as usize;
    let h = view.height as usize;
    let ch = view.format.channels();
    if w == 0 || h == 0 || view.stride < w * ch || view.data.len() < (h - 1) * view.stride + w * ch
    {
        return false;
    }
    let mut vals = Vec::with_capacity(((h / 4) + 1) * ((w / 4) + 1) * ch);
    for y in (0..h).step_by(4) {
        let row = &view.data[y * view.stride..];
        for x in (0..w).step_by(4) {
            for c in 0..ch {
                vals.push(row[x * ch + c]);
            }
        }
    }
    let std = std_f64_u8(&vals);
    let mean = mean_f64_u8(&vals);
    std < 12.0 || mean < 22.0 || mean > 238.0
}

// ---------------------------------------------------------------------------
// Exponential Backoff Scheduler & Single-Owner Daemon Worker
// ---------------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct LocalizationScheduler {
    pub tracking_interval: f64,
    pub lost_base_interval: f64,
    pub lost_max_interval: f64,
    pub backoff_factor: f64,
    last_run_at: Option<f64>,
    consecutive_misses: usize,
}

impl Default for LocalizationScheduler {
    fn default() -> Self {
        Self::new(0.10, 0.25, 2.0, 1.45)
    }
}

impl LocalizationScheduler {
    pub fn new(
        tracking_interval: f64,
        lost_base_interval: f64,
        lost_max_interval: f64,
        backoff_factor: f64,
    ) -> Self {
        Self {
            tracking_interval,
            lost_base_interval,
            lost_max_interval,
            backoff_factor,
            last_run_at: None,
            consecutive_misses: 0,
        }
    }

    #[inline]
    pub fn consecutive_misses(&self) -> usize {
        self.consecutive_misses
    }

    pub fn interval_for(&self, state: TrackerState) -> f64 {
        if state == TrackerState::Tracking {
            return self.tracking_interval;
        }
        let exp = self.consecutive_misses.min(8) as i32;
        let backoff = self.lost_base_interval * self.backoff_factor.powi(exp);
        backoff.min(self.lost_max_interval)
    }

    pub fn due(&self, now: f64, state: TrackerState) -> bool {
        self.last_run_at
            .is_none_or(|t| (now - t + 1e-9) >= self.interval_for(state))
    }

    pub fn mark_run(&mut self, now: f64) {
        self.last_run_at = Some(now);
    }

    pub fn observe_result(&mut self, found: bool, state: TrackerState) {
        if found || state == TrackerState::Tracking {
            self.consecutive_misses = 0;
        } else {
            self.consecutive_misses += 1;
        }
    }

    pub fn force_next(&mut self) {
        self.last_run_at = None;
        self.consecutive_misses = 0;
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct LiveResult {
    pub pose: MinimapPose,
    pub captured_at: f64,
    pub processing_ms: f64,
    pub generation: u64,
    pub search_mode: String,
}

pub fn visible_snapshot(
    result: Option<&LiveResult>,
    now: Option<f64>,
    max_age: f64,
) -> Option<MinimapPose> {
    let res = result?;
    let ts = now.unwrap_or(res.captured_at);
    if (ts - res.captured_at) > max_age {
        let mut clamped = res.pose.clone();
        clamped.state = TrackerState::Lost;
        clamped.world_x = None;
        clamped.world_y = None;
        clamped.lat = None;
        clamped.lng = None;
        clamped.confidence = 0.0;
        clamped.reason = Some("capture_expired".to_owned());
        Some(clamped)
    } else {
        Some(res.pose.clone())
    }
}

struct WorkerShared {
    generation: u64,
    paused: bool,
    latest: Option<LiveResult>,
    pending_anchor: Option<(i32, f64, f64, bool, Option<i32>, String)>,
    error: Option<String>,
}

pub type CaptureFrameFn = Box<dyn FnMut() -> Option<gh_core::frame::FrameBuf> + Send>;

pub struct LocalizationWorker {
    shared: Arc<Mutex<WorkerShared>>,
    stop_flag: Arc<AtomicBool>,
    cvar: Arc<Condvar>,
    max_age: f64,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    thread: Option<JoinHandle<()>>,
}

impl LocalizationWorker {
    pub fn new(
        mut tracker: HybridMinimapTracker,
        mut capture_fn: CaptureFrameFn,
        mut scheduler: LocalizationScheduler,
        max_age: f64,
        poll_sleep: Duration,
        clock: Option<Arc<dyn Fn() -> f64 + Send + Sync>>,
    ) -> Self {
        let start_inst = Instant::now();
        let clock_fn: Arc<dyn Fn() -> f64 + Send + Sync> =
            clock.unwrap_or_else(|| Arc::new(move || start_inst.elapsed().as_secs_f64()));
        let shared = Arc::new(Mutex::new(WorkerShared {
            generation: 0,
            paused: false,
            latest: None,
            pending_anchor: None,
            error: None,
        }));
        let stop_flag = Arc::new(AtomicBool::new(false));
        let cvar = Arc::new(Condvar::new());
        let poll_dur = poll_sleep.max(Duration::from_millis(1));

        let shared_w = Arc::clone(&shared);
        let stop_w = Arc::clone(&stop_flag);
        let cvar_w = Arc::clone(&cvar);
        let clock_w = Arc::clone(&clock_fn);

        let handle = std::thread::Builder::new()
            .name("genshin-helper-minimap-worker".to_owned())
            .spawn(move || {
                let mut seen_generation: u64 = u64::MAX;
                let mut was_paused = false;
                while !stop_w.load(Ordering::Acquire) {
                    let (current, paused, pending_anchor) = {
                        let mut g = shared_w.lock().expect("worker shared lock");
                        (g.generation, g.paused, g.pending_anchor.take())
                    };

                    if let Some((mid, wx, wy, is_ug, fid, fname)) = pending_anchor {
                        let now_ts = clock_w();
                        match tracker.set_map_m_anchor(
                            mid,
                            wx,
                            wy,
                            is_ug,
                            fid,
                            &fname,
                            Some(now_ts),
                        ) {
                            Ok(pose) => {
                                scheduler.force_next();
                                seen_generation = current;
                                let mut g = shared_w.lock().expect("worker shared lock");
                                if !stop_w.load(Ordering::Acquire)
                                    && current == g.generation
                                    && !g.paused
                                {
                                    g.latest = Some(LiveResult {
                                        pose,
                                        captured_at: now_ts,
                                        processing_ms: 0.0,
                                        generation: current,
                                        search_mode: "anchor".to_owned(),
                                    });
                                }
                            }
                            Err(e) => {
                                shared_w.lock().expect("worker shared lock").error =
                                    Some(e.to_string());
                                break;
                            }
                        }
                    }

                    if paused {
                        if !was_paused {
                            let _ = tracker.pause(Some(clock_w()), "worker_paused", 0.0);
                            was_paused = true;
                        }
                        let g = shared_w.lock().expect("worker shared lock");
                        if !stop_w.load(Ordering::Acquire) && g.paused && g.pending_anchor.is_none()
                        {
                            let _ = cvar_w.wait_timeout(g, Duration::from_millis(20));
                        }
                        continue;
                    }

                    if was_paused {
                        let _ = tracker.pause(Some(clock_w()), "worker_resumed", 0.0);
                        scheduler.force_next();
                        was_paused = false;
                    }

                    if current != seen_generation {
                        scheduler.force_next();
                        seen_generation = current;
                    }

                    let now_due = clock_w();
                    if !scheduler.due(now_due, tracker.state()) {
                        let g = shared_w.lock().expect("worker shared lock");
                        if !stop_w.load(Ordering::Acquire) && g.pending_anchor.is_none() {
                            let _ = cvar_w.wait_timeout(g, poll_dur);
                        }
                        continue;
                    }

                    let started = Instant::now();
                    let frame = capture_fn();
                    let captured_at = clock_w();
                    scheduler.mark_run(captured_at);

                    let (pose_res, search_mode, found) = match frame {
                        None => (
                            tracker.pause(Some(captured_at), "no_capture_frame", 0.0),
                            "paused".to_owned(),
                            false,
                        ),
                        Some(fb) => {
                            let fv = fb.as_view();
                            let gate_peek = tracker.gate.check(Some(fv), false);
                            if !gate_peek.valid {
                                let rsn = gate_peek
                                    .reason
                                    .unwrap_or_else(|| "minimap_not_visible".to_owned());
                                (
                                    tracker.pause(Some(captured_at), &rsn, 0.0),
                                    "gate_pause".to_owned(),
                                    false,
                                )
                            } else {
                                match tracker.step(Some(fv), Some(captured_at), None) {
                                    Ok(p) => {
                                        let sm = p.match_method.clone();
                                        let ok = p.state == TrackerState::Tracking
                                            && p.world_x.is_some();
                                        (Ok(p), sm, ok)
                                    }
                                    Err(e) => (Err(e), "error".to_owned(), false),
                                }
                            }
                        }
                    };

                    let pose = match pose_res {
                        Ok(p) => p,
                        Err(e) => {
                            shared_w.lock().expect("worker shared lock").error =
                                Some(e.to_string());
                            break;
                        }
                    };

                    scheduler.observe_result(found, pose.state);
                    let proc_ms = started.elapsed().as_secs_f64() * 1000.0;

                    let mut g = shared_w.lock().expect("worker shared lock");
                    if stop_w.load(Ordering::Acquire) || current != g.generation || g.paused {
                        continue;
                    }
                    g.latest = Some(LiveResult {
                        pose,
                        captured_at,
                        processing_ms: (proc_ms * 1000.0).round() / 1000.0,
                        generation: current,
                        search_mode,
                    });
                }
            })
            .ok();

        Self {
            shared,
            stop_flag,
            cvar,
            max_age,
            clock: clock_fn,
            thread: handle,
        }
    }

    pub fn generation(&self) -> u64 {
        self.shared.lock().expect("worker lock").generation
    }

    pub fn is_running(&self) -> bool {
        self.thread.as_ref().is_some_and(|h| !h.is_finished())
    }

    pub fn set_paused(&self, paused: bool) {
        let mut g = self.shared.lock().expect("worker lock");
        if paused != g.paused {
            g.paused = paused;
            g.generation += 1;
            g.latest = None;
            self.cvar.notify_all();
        }
    }

    pub fn anchor(
        &self,
        map_id: i32,
        world_x: f64,
        world_y: f64,
        is_underground: bool,
        floor_id: Option<i32>,
        floor_name: &str,
    ) {
        let mut g = self.shared.lock().expect("worker lock");
        g.pending_anchor = Some((
            map_id,
            world_x,
            world_y,
            is_underground,
            floor_id,
            floor_name.to_owned(),
        ));
        g.generation += 1;
        self.cvar.notify_all();
    }

    pub fn latest(&self) -> CvResult<Option<LiveResult>> {
        let g = self.shared.lock().expect("worker lock");
        if let Some(err) = &g.error {
            return Err(CvError::InvalidArgument(format!(
                "Localization worker failed: {err}"
            )));
        }
        Ok(g.latest.clone())
    }

    pub fn visible_snapshot(&self, now: Option<f64>, max_age: Option<f64>) -> CvResult<Option<MinimapPose>> {
        let res = self.latest()?;
        let ts = now.unwrap_or_else(|| (self.clock)());
        let age = max_age.unwrap_or(self.max_age);
        Ok(visible_snapshot(res.as_ref(), Some(ts), age))
    }

    pub fn stop(&mut self) {
        self.stop_flag.store(true, Ordering::Release);
        {
            let mut g = self.shared.lock().expect("worker lock");
            g.generation += 1;
            g.latest = None;
        }
        self.cvar.notify_all();
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for LocalizationWorker {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::locator::TerrainRaster;
    use crate::ops::morph::draw_filled_circle_u8;
    use crate::tile_cache::TileCacheConfig;
    use gh_core::frame::FrameBuf;
    use std::collections::BTreeMap;
    use std::path::Path;

    struct TempDirGuard(PathBuf);
    impl TempDirGuard {
        fn new(prefix: &str) -> Self {
            let p = std::env::temp_dir().join(format!(
                "{prefix}_{}_{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir_all(&p).unwrap();
            Self(p)
        }
    }
    impl Drop for TempDirGuard {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn make_empty_cache(tmp: &Path) -> LruTerrainTileCache {
        LruTerrainTileCache::from_config(TileCacheConfig {
            max_tiles: 16,
            tile_size: 256,
            npz_path: None,
            use_default_npz: false,
            disk_cache_dir: Some(tmp.join("tiles")),
            terrain_data: BTreeMap::new(),
            tile_provider: None,
            async_fetch: false,
        })
        .unwrap()
    }

    fn make_circular_minimap_from_gray(base: ArrayView2<'_, u8>) -> Array3<u8> {
        let (h, w) = base.dim();
        let mut bgr = Array3::<u8>::zeros((h, w, 3));
        let cx = w as f64 * 0.5;
        let cy = h as f64 * 0.5;
        let outer_r = (0.46 * h.min(w) as f64).round_ties_even();
        for y in 0..h {
            for x in 0..w {
                let rr = (x as f64 - cx).hypot(y as f64 - cy);
                if rr <= outer_r - 1.0 {
                    let v = base[[y, x]];
                    bgr[[y, x, 0]] = v;
                    bgr[[y, x, 1]] = v;
                    bgr[[y, x, 2]] = v;
                } else if (rr - outer_r).abs() <= 1.5 {
                    bgr[[y, x, 0]] = 215;
                    bgr[[y, x, 1]] = 224;
                    bgr[[y, x, 2]] = 232;
                } else {
                    bgr[[y, x, 0]] = 18;
                    bgr[[y, x, 1]] = 20;
                    bgr[[y, x, 2]] = 24;
                }
            }
        }
        bgr
    }

    fn make_synthetic_badge_bgr_and_mask() -> (Array3<u8>, Array2<u8>) {
        let (w, h) = (68usize, 77usize);
        let mut bgr = Array3::<u8>::zeros((h, w, 3));
        let mut mask = Array2::<u8>::zeros((h, w));
        for y in 0..h {
            let dy = (y as f64 - 38.0) / 35.0;
            for x in 0..w {
                let dx = (x as f64 - 33.5) / 30.0;
                if dx * dx + dy * dy <= 1.0 {
                    mask[[y, x]] = 255;
                }
                let rr = (x as f64 - 34.0).hypot(y as f64 - 38.0);
                if (rr - 26.0).abs() <= 2.0 {
                    bgr[[y, x, 0]] = 215;
                    bgr[[y, x, 1]] = 225;
                    bgr[[y, x, 2]] = 240;
                } else if rr <= 22.0 {
                    let stripe = if (x as isize - y as isize).abs() < 4 {
                        230
                    } else {
                        75
                    };
                    bgr[[y, x, 0]] = stripe;
                    bgr[[y, x, 1]] = (stripe as u16 / 2 + 40) as u8;
                    bgr[[y, x, 2]] = 165;
                } else {
                    bgr[[y, x, 0]] = 24;
                    bgr[[y, x, 1]] = 24;
                    bgr[[y, x, 2]] = 24;
                }
            }
        }
        (bgr, mask)
    }

    #[test]
    fn config_validation_rejects_invalid_bounds() {
        let c1 = MinimapTrackerConfig {
            smoothing_alpha: 0.0,
            ..Default::default()
        };
        assert!(c1.validate().is_err());

        let c2 = MinimapTrackerConfig {
            smoothing_alpha: 1.1,
            ..Default::default()
        };
        assert!(c2.validate().is_err());

        let c3 = MinimapTrackerConfig {
            acquire_hits: 0,
            ..Default::default()
        };
        assert!(c3.validate().is_err());

        let c4 = MinimapTrackerConfig {
            lost_timeout_seconds: 0.0,
            ..Default::default()
        };
        assert!(c4.validate().is_err());

        let c5 = MinimapTrackerConfig {
            max_phase_fallback_frames: 0,
            ..Default::default()
        };
        assert!(c5.validate().is_err());

        assert!(MinimapTrackerConfig::production_defaults().validate().is_ok());
    }

    #[test]
    fn minimap_gate_rejects_black_white_flat_and_unbordered() {
        let mut gate = MinimapGate::default();
        assert_eq!(gate.check(None, false).reason.as_deref(), Some("empty_frame"));

        let tiny = Array3::<u8>::zeros((20, 20, 3));
        assert_eq!(
            gate.check_array3(Some(tiny.view()), false).reason.as_deref(),
            Some("invalid_dimensions")
        );

        for fill in [0u8, 255, 120] {
            let flat = Array3::<u8>::from_elem((218, 218, 3), fill);
            let res = gate.check_array3(Some(flat.view()), false);
            assert!(!res.valid);
            assert_eq!(res.reason.as_deref(), Some("blank_or_flash_frame"));
        }

        // Unbordered square texture -> fails circular border check
        let mut tex = Array3::<u8>::zeros((218, 218, 3));
        for y in 0..218 {
            for x in 0..218 {
                let v = (((x * 13 + y * 7) % 180) + 30) as u8;
                tex[[y, x, 0]] = v;
                tex[[y, x, 1]] = v;
                tex[[y, x, 2]] = v;
            }
        }
        let unb = gate.check_array3(Some(tex.view()), false);
        assert!(!unb.valid);
        assert_eq!(unb.reason.as_deref(), Some("no_circular_minimap_border"));

        // Circular bordered HDR-washed frame
        let mut gray = Array2::<u8>::from_elem((218, 218), 252);
        for y in 40..170 {
            for x in 40..170 {
                if (x + y) % 14 < 5 {
                    gray[[y, x]] = 180;
                }
            }
        }
        let mm_hdr = make_circular_minimap_from_gray(gray.view());
        let r_hdr = gate.check_array3(Some(mm_hdr.view()), true);
        assert!(r_hdr.valid);
        assert!(r_hdr.hdr_clipped);

        // Identical second frame -> stationary == true
        let r_stat = gate.check_array3(Some(mm_hdr.view()), true);
        assert!(r_stat.valid);
        assert!(r_stat.stationary);
    }

    #[test]
    fn fsm_transitions_pause_and_velocity_gate() {
        let cfg = MinimapTrackerConfig {
            acquire_hits: 2,
            relocate_hits: 3,
            smoothing_alpha: 0.4,
            lost_timeout_seconds: 1.5,
            max_phase_fallback_frames: 5,
            ..MinimapTrackerConfig::default()
        };
        let mut fsm = TrackerFSM::new(cfg).unwrap();
        assert_eq!(fsm.state, TrackerState::Lost);

        let m1 = PoseMeasurement {
            found: true,
            map_id: Some(2),
            world_x: Some(100.0),
            world_y: Some(200.0),
            confidence: 0.85,
            match_method: "global".into(),
            ..PoseMeasurement::default()
        };
        let (acc1, _, _) = fsm.update(&m1, 10.0).unwrap();
        assert!(!acc1);
        assert_eq!(fsm.state, TrackerState::Acquiring);
        assert!(fsm.x.is_none());

        let m2 = PoseMeasurement {
            found: true,
            map_id: Some(2),
            world_x: Some(104.0),
            world_y: Some(204.0),
            confidence: 0.88,
            match_method: "global".into(),
            ..PoseMeasurement::default()
        };
        let (acc2, _, _) = fsm.update(&m2, 10.1).unwrap();
        assert!(acc2);
        assert_eq!(fsm.state, TrackerState::Tracking);
        assert!((fsm.x.unwrap() - 102.0).abs() < 1e-6);

        let m3 = PoseMeasurement {
            found: true,
            map_id: Some(2),
            world_x: Some(112.0),
            world_y: Some(202.0),
            confidence: 0.90,
            match_method: "annulus_template".into(),
            ..PoseMeasurement::default()
        };
        fsm.update(&m3, 10.2).unwrap();
        assert!((fsm.x.unwrap() - 106.0).abs() < 1e-6);

        // Pause freezes across 12s (> 1.5s timeout)
        fsm.pause(10.5).unwrap();
        fsm.pause(22.5).unwrap();
        let m_res = PoseMeasurement {
            found: true,
            map_id: Some(2),
            world_x: Some(106.0),
            world_y: Some(202.0),
            confidence: 0.85,
            match_method: "annulus_template".into(),
            ..PoseMeasurement::default()
        };
        fsm.update(&m_res, 22.6).unwrap();
        assert_eq!(fsm.state, TrackerState::Tracking);

        // Teleport outside velocity gate -> Relocating (3 hits)
        for (i, dt) in [(900.0, 22.7), (902.0, 22.8), (901.0, 22.9)] {
            let mt = PoseMeasurement {
                found: true,
                map_id: Some(2),
                world_x: Some(i),
                world_y: Some(900.0),
                confidence: 0.90,
                match_method: "global".into(),
                ..PoseMeasurement::default()
            };
            fsm.update(&mt, dt).unwrap();
        }
        assert_eq!(fsm.state, TrackerState::Tracking);
        assert!((fsm.x.unwrap() - 901.0).abs() < 2.0);

        // Timeout > 1.5s -> Lost
        let miss = PoseMeasurement::default();
        fsm.update(&miss, 23.5).unwrap();
        assert_eq!(fsm.state, TrackerState::Tracking);
        fsm.update(&miss, 24.6).unwrap();
        assert_eq!(fsm.state, TrackerState::Lost);
        assert!(fsm.x.is_none());
    }

    #[test]
    fn phase_correlate_budget_exhaustion_fails_safe_to_lost() {
        let tmp = TempDirGuard::new("gh_mt_phase");
        let cache = make_empty_cache(&tmp.0);
        let cfg = MinimapTrackerConfig {
            max_phase_fallback_frames: 5,
            hold_on_phase_exhausted: false,
            ..MinimapTrackerConfig::default()
        };
        let mut tracker = HybridMinimapTracker::new(cache, cfg).unwrap();
        let mut t = 100.0;
        tracker
            .set_map_m_anchor(2, 0.0, 0.0, false, None, "G", Some(t))
            .unwrap();

        let mut big_wave = Array2::<u8>::zeros((420, 420));
        for y in 0..420 {
            for x in 0..420 {
                let xf = x as f64;
                let yf = y as f64;
                let v = 120.0
                    + 32.0 * (xf * 0.17 + yf * 0.09).sin()
                    + 26.0 * (xf * 0.11 - yf * 0.21).cos()
                    + 18.0 * ((xf - 210.0).hypot(yf - 210.0) * 0.23).sin();
                big_wave[[y, x]] = v.clamp(20.0, 220.0) as u8;
            }
        }

        let mm0 = make_circular_minimap_from_gray(big_wave.slice(s![100..318, 100..318]));
        let g0 = bgr_to_gray(mm0.view()).unwrap();
        tracker.prev_phase_gray = tracker.prepare_phase_patch(g0.view(), None);

        let mut prev_x = tracker.fsm.x.unwrap();
        for k in 1..=5 {
            t += 0.1;
            let ox = 100 + k * 4;
            let mm_k = make_circular_minimap_from_gray(big_wave.slice(s![100..318, ox..ox + 218]));
            let pk = tracker.step_array3(Some(mm_k.view()), Some(t), None).unwrap();
            assert_eq!(pk.state, TrackerState::Tracking);
            assert_eq!(pk.match_method, "motion");
            assert!(pk.world_x.unwrap() > prev_x);
            prev_x = pk.world_x.unwrap();
        }

        t += 0.1;
        let ox = 100 + 6 * 4;
        let mm_6 = make_circular_minimap_from_gray(big_wave.slice(s![100..318, ox..ox + 218]));
        let p6 = tracker.step_array3(Some(mm_6.view()), Some(t), None).unwrap();
        assert_eq!(p6.state, TrackerState::Lost);
        assert!(p6.world_x.is_none());
        assert_eq!(p6.reason.as_deref(), Some("motion_fallback_budget_exceeded"));
    }

    #[test]
    fn paimon_matcher_auto_bootstrap_and_sobel_guard() {
        let tmp = TempDirGuard::new("gh_mt_paimon");
        let paimon_path = tmp.0.join("paimon_tpl.npz");

        // Build textured 1024x1024 synthetic raster so SIFT achieves >= 12 inliers cleanly
        let mut canvas = Array2::<u8>::zeros((1024, 1024));
        let mut st = 0x5555_1234_9999_0001u64;
        let mut next = || {
            st = st.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            st >> 32
        };
        for y in 0..1024 {
            for x in 0..1024 {
                let xf = x as f64;
                let yf = y as f64;
                let v = 115.0
                    + 35.0 * (xf * 0.17).sin() * (yf * 0.13).cos()
                    + 30.0 * (xf * 0.31 - yf * 0.27).cos()
                    + 25.0 * ((xf * 0.07).sin() + (yf * 0.09).cos());
                canvas[[y, x]] = v.clamp(20.0, 230.0) as u8;
            }
        }
        for _ in 0..1800 {
            let cx = (next() % 980 + 22) as i32;
            let cy = (next() % 980 + 22) as i32;
            let r = (next() % 4 + 1) as i32;
            let col = if (next() & 1) == 0 {
                (next() % 40 + 200) as u8
            } else {
                (next() % 40 + 15) as u8
            };
            draw_filled_circle_u8(&mut canvas, cx, cy, r, col);
        }

        let mut terrain = BTreeMap::new();
        terrain.insert(
            2,
            Arc::new(TerrainRaster {
                gray: canvas,
                meta: [0.0, 0.0, -1.0, 0.0, 0.0],
            }),
        );
        let cache = LruTerrainTileCache::from_config(TileCacheConfig {
            max_tiles: 16,
            tile_size: 256,
            npz_path: None,
            use_default_npz: false,
            disk_cache_dir: Some(tmp.0.join("tiles")),
            terrain_data: terrain,
            tile_provider: None,
            async_fetch: false,
        })
        .unwrap();

        let mut tracker = HybridMinimapTracker::new(
            Arc::new(cache),
            MinimapTrackerConfig::production_defaults(),
        )
        .unwrap()
        .with_paimon_cache_path(paimon_path.clone());

        let (badge, _) = make_synthetic_badge_bgr_and_mask();
        let lay = compute_ui_layout(1920, 1080, false).unwrap();
        let mut t = 10.0;
        let wx0 = 1024.0;
        let wy0 = 1024.0;
        tracker
            .set_map_m_anchor(2, wx0, wy0, false, None, "G", Some(t))
            .unwrap();

        let hud_w = (lay.minimap_rect.x + lay.minimap_rect.width + 48) as usize;
        let hud_h = (lay.minimap_rect.y + lay.minimap_rect.height + 28) as usize;

        for i in 1..=5 {
            t += 0.2;
            let (patch_opt, _) = tracker.tile_cache.extract_local_patch(
                2,
                wx0 + i as f64 * 12.0,
                wy0 + i as f64 * 8.0,
                180.0,
                218.0 / 360.0,
            );
            let patch = patch_opt.unwrap();
            let mm_sq = resize_u8(patch.view(), 218, 218, Interpolation::Linear).unwrap();
            let mm_bgr = make_circular_minimap_from_gray(mm_sq.view());
            let mut hud = Array3::<u8>::from_elem((hud_h, hud_w, 3), 24);
            let mx = lay.minimap_rect.x as usize;
            let my = lay.minimap_rect.y as usize;
            hud.slice_mut(s![my..my + 218, mx..mx + 218, ..])
                .assign(&mm_bgr);
            let px = lay.paimon_rect.x as usize;
            let py = lay.paimon_rect.y as usize;
            hud.slice_mut(s![py..py + 77, px..px + 68, ..])
                .assign(&badge);

            let pose = tracker
                .step_from_hud_array3(Some(hud.view()), 1920, 1080, Some(t), false, None)
                .unwrap();
            assert_eq!(pose.state, TrackerState::Tracking);
            if !tracker.has_paimon_tpl() {
                assert_ne!(
                    pose.match_method, "hd_sobel_fast",
                    "hd_sobel_fast must be skipped until Paimon bootstrap completes"
                );
            }
        }
        assert!(tracker.has_paimon_tpl(), "Paimon auto-bootstrap did not trigger");
        assert!(paimon_path.is_file());
        let npz = NpzArchive::read_file(&paimon_path).unwrap();
        assert!(npz.get("tpl_gray").is_some() && npz.get("mask").is_some());

        // Remove Paimon badge -> paimon_pause
        t += 0.2;
        let (patch_opt, _) =
            tracker
                .tile_cache
                .extract_local_patch(2, wx0 + 60.0, wy0 + 40.0, 180.0, 218.0 / 360.0);
        let mm_sq = resize_u8(patch_opt.unwrap().view(), 218, 218, Interpolation::Linear).unwrap();
        let mm_bgr = make_circular_minimap_from_gray(mm_sq.view());
        let mut hud_no_pm = Array3::<u8>::from_elem((hud_h, hud_w, 3), 24);
        let mx = lay.minimap_rect.x as usize;
        let my = lay.minimap_rect.y as usize;
        hud_no_pm
            .slice_mut(s![my..my + 218, mx..mx + 218, ..])
            .assign(&mm_bgr);
        let pause_pose = tracker
            .step_from_hud_array3(Some(hud_no_pm.view()), 1920, 1080, Some(t), false, None)
            .unwrap();
        assert_eq!(pause_pose.match_method, "paimon_pause");
        assert!(!pause_pose.should_broadcast_web);
    }

    #[test]
    fn is_ready_guard_does_not_increment_global_query_count() {
        let tmp = TempDirGuard::new("gh_mt_isready");
        let cache = make_empty_cache(&tmp.0);
        let reloc = Arc::new(TeyvatGlobalRelocator::new(
            None,
            Some(tmp.0.join("missing_v3.npz")),
            true,
        ));
        assert!(!reloc.is_ready());
        let mut tracker = HybridMinimapTracker::new(cache, MinimapTrackerConfig::default())
            .unwrap()
            .with_global_relocator(reloc);

        let mm = Array2::<u8>::from_elem((128, 128), 100);
        for k in 0..10 {
            let res = tracker.try_global_teyvat_relocate(
                mm.view(),
                None,
                None,
                None,
                128,
                1.0 + k as f64 * 0.1,
                1.0,
                0.0,
                Instant::now(),
            );
            assert!(res.is_none());
            assert_eq!(
                tracker.global_query_count, 0,
                "global_query_count must remain 0 while relocator is not ready"
            );
        }
    }

    #[test]
    fn localization_scheduler_and_visible_snapshot_staleness() {
        let mut sched = LocalizationScheduler::default();
        assert!((sched.interval_for(TrackerState::Tracking) - 0.10).abs() < 1e-9);
        assert!((sched.interval_for(TrackerState::Lost) - 0.25).abs() < 1e-9);
        let mut prev = 0.25;
        for _ in 0..5 {
            sched.observe_result(false, TrackerState::Lost);
            let nxt = sched.interval_for(TrackerState::Lost);
            assert!(nxt > prev || (nxt - 2.0).abs() < 1e-6);
            prev = nxt;
        }
        assert!(prev <= 2.0 + 1e-6);
        sched.observe_result(true, TrackerState::Tracking);
        assert_eq!(sched.consecutive_misses(), 0);
        assert!((sched.interval_for(TrackerState::Lost) - 0.25).abs() < 1e-9);

        let tmp = TempDirGuard::new("gh_mt_snap");
        let cache = make_empty_cache(&tmp.0);
        let mut tracker = HybridMinimapTracker::new(cache, MinimapTrackerConfig::default()).unwrap();
        let pose = tracker
            .set_map_m_anchor(2, 10.0, 20.0, false, None, "G", Some(1.0))
            .unwrap();
        let lr = LiveResult {
            pose,
            captured_at: 5.0,
            processing_ms: 1.2,
            generation: 1,
            search_mode: "anchor".into(),
        };
        let fresh = visible_snapshot(Some(&lr), Some(5.5), 1.5).unwrap();
        assert_eq!(fresh.state, TrackerState::Tracking);
        assert_eq!(fresh.world_x, Some(10.0));

        let stale = visible_snapshot(Some(&lr), Some(7.0), 1.5).unwrap();
        assert_eq!(stale.state, TrackerState::Lost);
        assert!(stale.world_x.is_none());
        assert_eq!(stale.reason.as_deref(), Some("capture_expired"));
    }

    #[test]
    fn localization_worker_mailbox_anchor_pause_and_shutdown() {
        let tmp = TempDirGuard::new("gh_mt_worker");
        let cache = make_empty_cache(&tmp.0);
        let tracker = HybridMinimapTracker::new(cache, MinimapTrackerConfig::default()).unwrap();
        let cap: CaptureFrameFn = Box::new(|| {
            Some(FrameBuf::new_zeroed(218, 218, PixelFormat::Bgra8))
        });
        let mut worker = LocalizationWorker::new(
            tracker,
            cap,
            LocalizationScheduler::new(0.02, 0.05, 0.5, 1.45),
            1.5,
            Duration::from_millis(5),
            None,
        );
        assert!(worker.is_running());
        worker.anchor(2, -500.0, -1266.0, false, None, "G");

        let deadline = Instant::now() + Duration::from_secs(2);
        let mut got = None;
        while Instant::now() < deadline {
            if let Ok(Some(res)) = worker.latest() {
                got = Some(res);
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let res = got.expect("worker failed to publish anchor");
        assert_eq!(res.pose.state, TrackerState::Tracking);

        let g0 = worker.generation();
        worker.set_paused(true);
        assert_eq!(worker.generation(), g0 + 1);
        assert!(worker.latest().unwrap().is_none());

        worker.set_paused(false);
        assert_eq!(worker.generation(), g0 + 2);
        worker.stop();
        assert!(!worker.is_running());
    }
}
