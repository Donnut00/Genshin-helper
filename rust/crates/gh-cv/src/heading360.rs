//! 3-tier camera cone + 3-tip player arrow 360-degree heading estimator & `PureOpenCVTracker`
//! (`src/heading360.py` + `src/tracker.py`).

use crate::ops::color::{
    bgr_px_to_gray, bgr_to_hsv, frame_view_to_array3, in_range, swap_rb,
};
use crate::ops::contours::{
    bounding_rect, canny_u8, connected_components_with_stats_8, contour_area, contour_moments,
    find_contours_external,
};
use crate::ops::morph::{
    dilate_u8, draw_filled_circle_u8, erode_u8, get_structuring_element, morphology_ex_u8,
};
use crate::ops::resize::{copy_make_border_f32, copy_make_border_u8, resize_u8, resize_u8_hwc};
use crate::ops::sobel::{scharr_dy_f32, scharr_dy_u8_f32};
use crate::ops::warp::{warp_polar_annulus_u8, warp_polar_annulus_u8_hwc};
use crate::ops::{
    BorderMode, ContourMethod, CvError, CvResult, Interpolation, MorphOp, MorphShape, Point2i,
};
use gh_core::frame::{FrameView, PixelFormat};
use ndarray::{Array2, Array3, ArrayView2, ArrayView3, s};
use std::sync::OnceLock;

pub const POLAR_BINS: usize = 512;
pub const RAD_BINS: usize = 128;
pub const RAD_R0: usize = 26;
pub const RAD_R1: usize = 102;
pub const CONE_BLEND_ALPHA: f64 = 0.25;
pub const CONE_ROLL_SHIFT: usize = 5;

pub const CYAN_HSV_LOWER: [u8; 3] = [85, 160, 180];
pub const CYAN_HSV_UPPER: [u8; 3] = [105, 255, 255];
pub const YELLOW_PING_HSV_LOWER: [u8; 3] = [20, 120, 180];
pub const YELLOW_PING_HSV_UPPER: [u8; 3] = [35, 255, 255];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum ConeMethod {
    #[default]
    None,
    BgraAlpha,
    BgrBlend,
    HsvAdaptive,
}

impl ConeMethod {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::BgraAlpha => "bgra_alpha",
            Self::BgrBlend => "bgr_blend",
            Self::HsvAdaptive => "hsv_adaptive",
        }
    }

    pub const fn is_trusted(self) -> bool {
        matches!(self, Self::BgraAlpha | Self::BgrBlend)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum FuserSource {
    #[default]
    None,
    BgraAlpha,
    BgrBlend,
    HsvAdaptive,
    Body,
}

impl FuserSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::BgraAlpha => "bgra_alpha",
            Self::BgrBlend => "bgr_blend",
            Self::HsvAdaptive => "hsv_adaptive",
            Self::Body => "body",
        }
    }
}

impl From<ConeMethod> for FuserSource {
    fn from(m: ConeMethod) -> Self {
        match m {
            ConeMethod::None => Self::None,
            ConeMethod::BgraAlpha => Self::BgraAlpha,
            ConeMethod::BgrBlend => Self::BgrBlend,
            ConeMethod::HsvAdaptive => Self::HsvAdaptive,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct ConeDetection {
    pub heading_deg: Option<f64>,
    pub confidence: f64,
    pub method: ConeMethod,
}

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct ArrowDetection {
    pub heading_deg: Option<f64>,
    pub confidence: f64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FullscreenPlayerPose {
    pub pixel_x: f64,
    pub pixel_y: f64,
    pub angle_deg: f64,
}

#[inline]
pub fn wrap_angle_360(deg: f64) -> f64 {
    if !deg.is_finite() {
        return 0.0;
    }
    let val = deg.rem_euclid(360.0);
    if val >= 360.0 || val == 0.0 {
        0.0
    } else {
        val
    }
}

#[inline]
pub fn shortest_angle_delta(from_deg: f64, to_deg: f64) -> f64 {
    let delta = (to_deg - from_deg + 180.0).rem_euclid(360.0) - 180.0;
    if delta == -180.0 {
        180.0
    } else if delta == 0.0 {
        0.0
    } else {
        delta
    }
}

pub fn compute_nav_relative_angle(
    player_pos: [f64; 2],
    target_pos: [f64; 2],
    view_deg_cw_north: f64,
) -> (f64, f64) {
    let dx = target_pos[0] - player_pos[0];
    let dy = target_pos[1] - player_pos[1];
    let dist = dx.hypot(dy);
    if dist < 1e-9 {
        return (0.0, 0.0);
    }
    let target_bearing_deg = wrap_angle_360(dx.atan2(-dy).to_degrees());
    let delta_deg = shortest_angle_delta(view_deg_cw_north, target_bearing_deg);
    (delta_deg, dist)
}

fn lut_pred_u8() -> &'static [u8; 256] {
    static LUT: OnceLock<[u8; 256]> = OnceLock::new();
    LUT.get_or_init(|| {
        let mut out = [0u8; 256];
        for (i, slot) in out.iter_mut().enumerate() {
            *slot = ((i as f64 * 0.75 + 63.75).round_ties_even() as i32).clamp(0, 255) as u8;
        }
        out
    })
}

fn morph_rect_3x3() -> &'static Array2<u8> {
    static K: OnceLock<Array2<u8>> = OnceLock::new();
    K.get_or_init(|| get_structuring_element(MorphShape::Rect, 3, 3).expect("3x3 rect"))
}

fn morph_rect_2x2() -> &'static Array2<u8> {
    static K: OnceLock<Array2<u8>> = OnceLock::new();
    K.get_or_init(|| get_structuring_element(MorphShape::Rect, 2, 2).expect("2x2 rect"))
}

fn morph_ellipse_3x3() -> &'static Array2<u8> {
    static K: OnceLock<Array2<u8>> = OnceLock::new();
    K.get_or_init(|| get_structuring_element(MorphShape::Ellipse, 3, 3).expect("3x3 ellipse"))
}

#[inline]
fn circular_boxcar_5(vec: &[f32; POLAR_BINS]) -> [f32; POLAR_BINS] {
    let mut out = [0.0f32; POLAR_BINS];
    for i in 0..POLAR_BINS {
        let i_m2 = (i + POLAR_BINS - 2) % POLAR_BINS;
        let i_m1 = (i + POLAR_BINS - 1) % POLAR_BINS;
        let i_p1 = (i + 1) % POLAR_BINS;
        let i_p2 = (i + 2) % POLAR_BINS;
        out[i] = (vec[i_m2] + vec[i_m1] + vec[i] + vec[i_p1] + vec[i_p2]) * 0.2f32;
    }
    out
}

#[inline]
fn circular_subpixel_peak(arr: &[f32; POLAR_BINS], idx: usize, negate: bool) -> f64 {
    let sign = if negate { -1.0f64 } else { 1.0f64 };
    let y_prev = arr[(idx + POLAR_BINS - 1) % POLAR_BINS] as f64 * sign;
    let y_curr = arr[idx] as f64 * sign;
    let y_next = arr[(idx + 1) % POLAR_BINS] as f64 * sign;
    let denom = y_prev - 2.0 * y_curr + y_next;
    let offset = if denom.abs() > 1e-9 {
        (0.5 * (y_prev - y_next) / denom).clamp(-0.5, 0.5)
    } else {
        0.0
    };
    (idx as f64 + offset).rem_euclid(POLAR_BINS as f64)
}

#[inline]
fn circular_bin_dist(a: usize, b: usize) -> usize {
    let d1 = (a + POLAR_BINS - b) % POLAR_BINS;
    let d2 = (b + POLAR_BINS - a) % POLAR_BINS;
    d1.min(d2)
}

fn wedge_brightness_ok(v_prof: &[f32; POLAR_BINS], center_bin: usize) -> bool {
    let mut in_sum = 0.0f64;
    let mut in_cnt = 0usize;
    let mut out_sum = 0.0f64;
    let mut out_cnt = 0usize;
    for (idx, &v) in v_prof.iter().enumerate() {
        let cd = circular_bin_dist(idx, center_bin);
        if cd <= 56 {
            in_sum += v as f64;
            in_cnt += 1;
        } else if (72..=112).contains(&cd) {
            out_sum += v as f64;
            out_cnt += 1;
        }
    }
    if in_cnt == 0 || out_cnt == 0 {
        return false;
    }
    let inside_v = in_sum / in_cnt as f64;
    let outside_v = out_sum / out_cnt as f64;
    inside_v > outside_v + 0.01
}

fn extract_cone_bgra_alpha_polar(alpha_channel: ArrayView2<'_, u8>) -> Option<(f64, f64)> {
    let mut a_min = 255u8;
    let mut a_max = 0u8;
    for &v in alpha_channel.iter() {
        a_min = a_min.min(v);
        a_max = a_max.max(v);
    }
    if a_min == 255 || a_max <= a_min {
        return None;
    }

    let (h, w) = alpha_channel.dim();
    let center = (w as f64 * 0.5, h as f64 * 0.5);
    let max_rad = (h.min(w) as f64) * 0.5;
    let ann_a = warp_polar_annulus_u8(
        alpha_channel,
        RAD_BINS,
        POLAR_BINS,
        (RAD_R0, RAD_R1),
        center,
        max_rad,
        Interpolation::Linear,
    )
    .ok()?;
    let h_rad = ann_a.dim().1;
    let padded = copy_make_border_u8(ann_a.view(), 2, 2, 0, 0, BorderMode::Wrap).ok()?;
    let grad_y = scharr_dy_u8_f32(padded.view()).ok()?;

    let mut row_sums = [0.0f32; POLAR_BINS];
    for (y, dst) in row_sums.iter_mut().enumerate() {
        let mut s = 0.0f32;
        for x in 0..h_rad {
            s += grad_y[[y + 2, x]];
        }
        *dst = s;
    }
    let col_sum = circular_boxcar_5(&row_sums);

    let mut min_idx = 0usize;
    let mut max_idx = 0usize;
    let mut min_val = f32::INFINITY;
    let mut max_val = f32::NEG_INFINITY;
    for (i, &v) in col_sum.iter().enumerate() {
        if v < min_val {
            min_val = v;
            min_idx = i;
        }
        if v > max_val {
            max_val = v;
            max_idx = i;
        }
    }

    let min_edge = 16.0 * (h_rad as f64) * 4.0;
    if (min_val as f64).abs() < min_edge || (max_val as f64).abs() < min_edge {
        return None;
    }

    let ang_min = (circular_subpixel_peak(&col_sum, min_idx, true) / POLAR_BINS as f64) * 360.0;
    let ang_max = (circular_subpixel_peak(&col_sum, max_idx, false) / POLAR_BINS as f64) * 360.0;
    let span_low = (ang_max - ang_min).rem_euclid(360.0);
    let span_high = (ang_min - ang_max).rem_euclid(360.0);

    let (mid_polar, span_err) = if (span_low - 90.0).abs() <= 15.0 {
        (ang_min + span_low * 0.5, (span_low - 90.0).abs())
    } else if (span_high - 90.0).abs() <= 15.0 {
        (ang_max + span_high * 0.5, (span_high - 90.0).abs())
    } else {
        return None;
    };

    let heading_cw = wrap_angle_360(mid_polar + 90.0);
    let strength = (min_val as f64).abs().min((max_val as f64).abs()) / (16.0 * h_rad as f64 * 40.0);
    let fov_sc = (1.0 - span_err / 20.0).max(0.0);
    let conf = (0.55 + 0.30 * strength.min(1.0) + 0.14 * fov_sc).clamp(0.55, 0.99);
    Some((heading_cw, conf))
}

fn extract_cone_bgr_blend_simd(
    ann: ArrayView3<'_, u8>,
    v_prof: &[f32; POLAR_BINS],
) -> Option<(f64, f64)> {
    let lut = lut_pred_u8();
    let h_rad = ann.dim().1;
    let ann_cow = ann.as_standard_layout();
    let ann_s = ann_cow.as_slice()?;
    let mut ann_gray = vec![0u8; POLAR_BINS * h_rad];
    for (i, dst) in ann_gray.iter_mut().enumerate() {
        let p = i * 3;
        *dst = bgr_px_to_gray(ann_s[p], ann_s[p + 1], ann_s[p + 2]);
    }
    let mut m_row = vec![0u8; h_rad];
    let mut e_row = vec![0u8; h_rad];

    let mut angles = [0.0f64; 2];
    let mut pvals = [0.0f64; 2];
    let mut snrs = [0.0f64; 2];

    // Loop order matches Python `for off in (2 * _CONE_ROLL_SHIFT, 0)`:
    // iter 0 -> row (y + 5) % 512 (roll(-5)); iter 1 -> row (y - 5) % 512 (roll(+5)).
    for (pass_idx, shift) in [CONE_ROLL_SHIFT as isize, -(CONE_ROLL_SHIFT as isize)]
        .into_iter()
        .enumerate()
    {
        let mut row_sums = [0.0f32; POLAR_BINS];
        for (y, sum_dst) in row_sums.iter_mut().enumerate() {
            let sy = (y as isize + shift).rem_euclid(POLAR_BINS as isize) as usize;
            let row_off = y * h_rad;
            let srow_off = sy * h_rad;
            for x in 0..h_rad {
                let p = (row_off + x) * 3;
                let sp = (srow_off + x) * 3;
                let db = (lut[ann_s[p] as usize] as i16 - ann_s[sp] as i16).abs();
                let dg = (lut[ann_s[p + 1] as usize] as i16 - ann_s[sp + 1] as i16).abs();
                let dr = (lut[ann_s[p + 2] as usize] as i16 - ann_s[sp + 2] as i16).abs();
                m_row[x] = if db <= 8
                    && dg <= 8
                    && dr <= 8
                    && ann_gray[srow_off + x] > ann_gray[row_off + x].saturating_add(3)
                {
                    255
                } else {
                    0
                };
            }
            // Bit-exact MorphOp::Open with Rect(2, 1) (anchor (1, 0): offsets (-1, 0), (0, 0))
            e_row[0] = m_row[0];
            for x in 1..h_rad {
                e_row[x] = m_row[x - 1] & m_row[x];
            }
            let mut sum_u32 = e_row[0] as u32;
            for x in 1..h_rad {
                sum_u32 += (e_row[x - 1] | e_row[x]) as u32;
            }
            *sum_dst = sum_u32 as f32;
        }
        let cs = circular_boxcar_5(&row_sums);
        let mut pk_i = 0usize;
        let mut pk = f32::NEG_INFINITY;
        let mut total = 0.0f64;
        for (i, &v) in cs.iter().enumerate() {
            total += v as f64;
            if v > pk {
                pk = v;
                pk_i = i;
            }
        }
        let pk_f = pk as f64;
        let mn = total / POLAR_BINS as f64;
        if pk_f < 0.04 * (h_rad as f64) * 255.0 || pk_f < 2.2 * (mn + 1e-3) {
            return None;
        }
        angles[pass_idx] = (circular_subpixel_peak(&cs, pk_i, false) / POLAR_BINS as f64) * 360.0;
        pvals[pass_idx] = pk_f / (h_rad as f64 * 255.0);
        snrs[pass_idx] = pk_f / (mn + 1e-3);
    }

    let span = (angles[1] - angles[0]).rem_euclid(360.0);
    if (span - 90.0).abs() > 20.0 {
        return None;
    }

    let mid_polar = angles[0] + span * 0.5;
    let mid_bin = ((mid_polar.rem_euclid(360.0) / 360.0 * POLAR_BINS as f64).round_ties_even()
        as usize)
        % POLAR_BINS;
    if !wedge_brightness_ok(v_prof, mid_bin) {
        return None;
    }

    let heading_cw = wrap_angle_360(mid_polar + 90.0);
    let fill_sc = (pvals[0].min(pvals[1]) / 0.35).min(1.0);
    let snr_sc = ((snrs[0].min(snrs[1]) - 2.0) / 10.0).min(1.0);
    let fov_sc = (1.0 - (span - 90.0).abs() / 25.0).max(0.0);
    let conf = (0.45 + 0.25 * fill_sc + 0.15 * snr_sc + 0.10 * fov_sc).clamp(0.45, 0.95);
    Some((heading_cw, conf))
}

fn extract_cone_hsv_adaptive_polar(
    ann: ArrayView3<'_, u8>,
    v_prof: &[f32; POLAR_BINS],
) -> Option<(f64, f64)> {
    let hsv = bgr_to_hsv(ann).ok()?;
    let h_rad = ann.dim().1;
    let mut contrast = Array2::<f32>::zeros((POLAR_BINS, h_rad));
    let sat_scale = 0.25f32 / 255.0f32;
    let val_scale = 1.0f32 / 255.0f32;
    for y in 0..POLAR_BINS {
        for x in 0..h_rad {
            let v = hsv[[y, x, 2]] as f32 * val_scale;
            let s = hsv[[y, x, 1]] as f32 * sat_scale;
            contrast[[y, x]] = v - s;
        }
    }

    let padded = copy_make_border_f32(contrast.view(), 2, 2, 0, 0, BorderMode::Wrap).ok()?;
    let gy_full = scharr_dy_f32(padded.view()).ok()?;
    let mut row_means = [0.0f32; POLAR_BINS];
    let inv_16 = 1.0f32 / 16.0f32;
    let inv_rad = 1.0f32 / h_rad as f32;
    for (y, dst) in row_means.iter_mut().enumerate() {
        let mut s = 0.0f32;
        for x in 0..h_rad {
            s += (gy_full[[y + 2, x]] * inv_16).clamp(-0.35, 0.35);
        }
        *dst = s * inv_rad;
    }
    let g = circular_boxcar_5(&row_means);

    let mut best_score = [0.0f32; POLAR_BINS];
    for half_fov in [60usize, 64, 68] {
        for i in 0..POLAR_BINS {
            let enter = g[(i + POLAR_BINS - half_fov) % POLAR_BINS].max(0.0);
            let exit = (-g[(i + half_fov) % POLAR_BINS]).max(0.0);
            let sc = (enter * exit).sqrt();
            if sc > best_score[i] {
                best_score[i] = sc;
            }
        }
    }

    let mut best_idx = 0usize;
    let mut peak = f32::NEG_INFINITY;
    for (i, &v) in best_score.iter().enumerate() {
        if v > peak {
            peak = v;
            best_idx = i;
        }
    }
    let peak_f = peak as f64;
    if peak_f < 0.015 {
        return None;
    }

    let mut bg: Vec<f64> = Vec::with_capacity(POLAR_BINS);
    for (i, &v) in best_score.iter().enumerate() {
        if circular_bin_dist(i, best_idx) > 24 {
            bg.push(v as f64);
        }
    }
    if bg.is_empty() {
        return None;
    }
    let n = bg.len();
    let mean = bg.iter().sum::<f64>() / n as f64;
    let var = bg.iter().map(|&x| (x - mean) * (x - mean)).sum::<f64>() / n as f64;
    let bg_std = var.sqrt();

    bg.sort_by(|a, b| a.total_cmp(b));
    let bg_med = if n.is_multiple_of(2) {
        0.5 * (bg[n / 2 - 1] + bg[n / 2])
    } else {
        bg[n / 2]
    };
    let pos_98 = 0.98 * (n - 1) as f64;
    let lo = pos_98.floor() as usize;
    let hi = pos_98.ceil() as usize;
    let frac = pos_98 - lo as f64;
    let bg_p98 = bg[lo] * (1.0 - frac) + bg[hi] * frac;

    if peak_f <= bg_med + 3.5 * bg_std.max(1e-4) || peak_f <= 1.2 * bg_p98 {
        return None;
    }
    if !wedge_brightness_ok(v_prof, best_idx) {
        return None;
    }

    let c_sub = circular_subpixel_peak(&best_score, best_idx, false);
    let heading_cw = wrap_angle_360((c_sub / POLAR_BINS as f64) * 360.0 + 90.0);
    let prom = (peak_f - bg_med) / (peak_f + 0.05);
    let conf = (0.35 + 0.47 * prom).clamp(0.35, 0.82);
    Some((heading_cw, conf))
}

pub fn extract_camera_view_cone(minimap_img: ArrayView3<'_, u8>) -> ConeDetection {
    let (h, w, c) = minimap_img.dim();
    if (c != 3 && c != 4) || h.min(w) < 24 {
        return ConeDetection::default();
    }

    // Tier 1: BGRA alpha-channel fast path
    if c == 4 {
        let alpha = minimap_img.slice(s![.., .., 3]);
        if let Some((deg, conf)) = extract_cone_bgra_alpha_polar(alpha) {
            return ConeDetection {
                heading_deg: Some(deg),
                confidence: conf,
                method: ConeMethod::BgraAlpha,
            };
        }
    }

    let bgr = if c == 4 {
        minimap_img.slice(s![.., .., 0..3])
    } else {
        minimap_img
    };
    let center = (w as f64 * 0.5, h as f64 * 0.5);
    let max_rad = (h.min(w) as f64) * 0.5;
    let Ok(ann) = warp_polar_annulus_u8_hwc(
        bgr,
        RAD_BINS,
        POLAR_BINS,
        (RAD_R0, RAD_R1),
        center,
        max_rad,
        Interpolation::Linear,
    ) else {
        return ConeDetection::default();
    };

    // Exact V profile (= max(B, G, R) / 255.0 averaged per row) without computing full HSV upfront
    let h_rad = ann.dim().1;
    let mut v_prof = [0.0f32; POLAR_BINS];
    let inv_norm = 1.0 / (255.0 * h_rad as f64);
    for (y, dst) in v_prof.iter_mut().enumerate() {
        let mut sum_v = 0u64;
        for x in 0..h_rad {
            let v = ann[[y, x, 0]].max(ann[[y, x, 1]]).max(ann[[y, x, 2]]);
            sum_v += v as u64;
        }
        *dst = (sum_v as f64 * inv_norm) as f32;
    }

    if let Some((deg, conf)) = extract_cone_bgr_blend_simd(ann.view(), &v_prof) {
        return ConeDetection {
            heading_deg: Some(deg),
            confidence: conf,
            method: ConeMethod::BgrBlend,
        };
    }

    if let Some((deg, conf)) = extract_cone_hsv_adaptive_polar(ann.view(), &v_prof) {
        return ConeDetection {
            heading_deg: Some(deg),
            confidence: conf,
            method: ConeMethod::HsvAdaptive,
        };
    }

    ConeDetection::default()
}

fn frame_view_to_bgr_or_bgra(frame: FrameView<'_>) -> Option<Array3<u8>> {
    match frame.format {
        PixelFormat::Gray8 => None,
        PixelFormat::Bgr8 | PixelFormat::Bgra8 => frame_view_to_array3(frame).ok(),
        PixelFormat::Rgba8 => {
            let rgba = frame_view_to_array3(frame).ok()?;
            swap_rb(rgba.view()).ok()
        }
    }
}

pub fn extract_camera_view_cone_frame(frame: FrameView<'_>) -> ConeDetection {
    let Some(arr) = frame_view_to_bgr_or_bgra(frame) else {
        return ConeDetection::default();
    };
    extract_camera_view_cone(arr.view())
}

fn clean_arrow_mask(mask2: ArrayView2<'_, u8>) -> Array2<u8> {
    let Ok(cc) = connected_components_with_stats_8(mask2) else {
        return mask2.to_owned();
    };
    if cc.num_labels <= 2 {
        return mask2.to_owned();
    }
    let (mh, mw) = mask2.dim();
    let cx0 = mw as f64 / 2.0;
    let cy0 = mh as f64 / 2.0;
    let max_dist = 0.42 * (mh.min(mw) as f64);

    let mut best_label: Option<usize> = None;
    let mut best_area = 0i32;
    for lbl in 1..cc.num_labels {
        let area = cc.stats[lbl].area;
        let dist = (cc.centroids[lbl][0] - cx0).hypot(cc.centroids[lbl][1] - cy0);
        if dist <= max_dist && area > best_area {
            best_area = area;
            best_label = Some(lbl);
        }
    }
    let Some(best_lbl) = best_label else {
        return mask2.to_owned();
    };

    let min_keep_area = 4.max((0.12 * best_area as f64) as i32);
    let mut keep = vec![false; cc.num_labels];
    for (lbl, slot) in keep.iter_mut().enumerate().skip(1) {
        let area = cc.stats[lbl].area;
        let dist = (cc.centroids[lbl][0] - cx0).hypot(cc.centroids[lbl][1] - cy0);
        if lbl == best_lbl || (dist <= max_dist && area >= min_keep_area) {
            *slot = true;
        }
    }
    let mut cleaned = Array2::<u8>::zeros((mh, mw));
    for ((dst, &lbl), _) in cleaned.iter_mut().zip(cc.labels.iter()).zip(mask2.iter()) {
        if lbl > 0 && keep[lbl as usize] {
            *dst = 255;
        }
    }
    cleaned
}

fn erase_core_and_find_contours(
    edges_base: ArrayView2<'_, u8>,
    cx: f64,
    cy: f64,
    r_erase: f64,
) -> Vec<Vec<Point2i>> {
    let mut work = edges_base.to_owned();
    draw_filled_circle_u8(
        &mut work,
        cx.ceil() as i32,
        cy.ceil() as i32,
        (r_erase.ceil() as i32).max(1),
        0,
    );
    let Ok(dilated) = dilate_u8(work.view(), morph_rect_3x3().view()) else {
        return Vec::new();
    };
    let Ok(eroded) = erode_u8(dilated.view(), morph_rect_2x2().view()) else {
        return Vec::new();
    };
    let Ok(mut contours) = find_contours_external(eroded.view(), ContourMethod::None) else {
        return Vec::new();
    };
    if contours.len() > 3 {
        let filtered: Vec<_> = contours.iter().filter(|c| c.len() >= 6).cloned().collect();
        if !filtered.is_empty() {
            contours = filtered;
        }
    }
    contours
}

#[inline]
fn contour_bbox_center(c: &[Point2i]) -> [f64; 2] {
    let r = bounding_rect(c);
    [
        r.x as f64 + r.width as f64 / 2.0,
        r.y as f64 + r.height as f64 / 2.0,
    ]
}

fn is_symmetric_two_wings(cnts: &[Vec<Point2i>], img_center: [f64; 2]) -> bool {
    if cnts.len() != 2 {
        return false;
    }
    let p0 = contour_bbox_center(&cnts[0]);
    let p1 = contour_bbox_center(&cnts[1]);
    let v0 = [p0[0] - img_center[0], p0[1] - img_center[1]];
    let v1 = [p1[0] - img_center[0], p1[1] - img_center[1]];
    let d0 = v0[0].hypot(v0[1]);
    let d1 = v1[0].hypot(v1[1]);
    if d0.min(d1) < 2.0 {
        return false;
    }
    let ratio = d0.max(d1) / d0.min(d1);
    let cos_ang = ((v0[0] * v1[0] + v0[1] * v1[1]) / (d0 * d1)).clamp(-1.0, 1.0);
    let subtended_deg = cos_ang.acos().to_degrees();
    let mid = [0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1])];
    let mid_dist = (img_center[0] - mid[0]).hypot(img_center[1] - mid[1]);
    ratio <= 1.35 && (40.0..=120.0).contains(&subtended_deg) && mid_dist >= 2.0
}

fn solve_arrow_from_mask(mask2: ArrayView2<'_, u8>, conf_scale: f64) -> Option<(f64, f64)> {
    let nz_raw = mask2.iter().filter(|&&v| v != 0).count();
    if nz_raw < 16 {
        return None;
    }
    let clean2 = clean_arrow_mask(mask2);
    let nz_clean = clean2.iter().filter(|&&v| v != 0).count();
    if nz_clean < 16 {
        return None;
    }

    let (mh, mw) = clean2.dim();
    let mask4 = resize_u8(clean2.view(), mw * 2, mh * 2, Interpolation::Area).ok()?;
    let edges_base = canny_u8(mask4.view(), 20.0, 60.0).ok()?;
    let (rows, cols) = mask4.dim();
    let img_center = [cols as f64 / 2.0, rows as f64 / 2.0];

    let mut contours = erase_core_and_find_contours(
        edges_base.view(),
        img_center[0],
        img_center[1],
        cols as f64 / 4.5,
    );

    if contours.len() != 3 && !is_symmetric_two_wings(&contours, img_center) {
        let mut sum_x = 0.0f64;
        let mut sum_y = 0.0f64;
        let mut pts_nz: Vec<(f64, f64)> = Vec::new();
        for y in 0..rows {
            for x in 0..cols {
                if mask4[[y, x]] != 0 {
                    let fx = x as f64;
                    let fy = y as f64;
                    sum_x += fx;
                    sum_y += fy;
                    pts_nz.push((fx, fy));
                }
            }
        }
        if pts_nz.len() >= 16 {
            let n = pts_nz.len() as f64;
            let mcx = sum_x / n;
            let mcy = sum_y / n;
            let mut r_max = 0.0f64;
            for &(fx, fy) in &pts_nz {
                r_max = r_max.max((fx - mcx).hypot(fy - mcy));
            }
            if r_max >= 6.0 {
                let mut fallback_two: Option<Vec<Vec<Point2i>>> = None;
                let mut found_three = false;
                for frac in [0.62, 0.56, 0.68, 0.50, 0.74, 0.44] {
                    let cand =
                        erase_core_and_find_contours(edges_base.view(), mcx, mcy, frac * r_max);
                    if cand.len() == 3 {
                        contours = cand;
                        found_three = true;
                        break;
                    }
                    if cand.len() == 2 && fallback_two.is_none() {
                        fallback_two = Some(cand);
                    }
                }
                if !found_three
                    && contours.len() != 2
                    && let Some(fb) = fallback_two
                {
                    contours = fb;
                }
            }
        }
    }

    if contours.len() == 3 {
        let pts = [
            contour_bbox_center(&contours[0]),
            contour_bbox_center(&contours[1]),
            contour_bbox_center(&contours[2]),
        ];
        let d01 = (pts[1][0] - pts[0][0]).hypot(pts[1][1] - pts[0][1]);
        let d02 = (pts[2][0] - pts[0][0]).hypot(pts[2][1] - pts[0][1]);
        let d12 = (pts[2][0] - pts[1][0]).hypot(pts[2][1] - pts[1][1]);

        let (nose, w0, w1, short_d, l0, l1) = if d01 <= d02 && d01 <= d12 {
            (pts[2], pts[0], pts[1], d01, d02, d12)
        } else if d02 <= d01 && d02 <= d12 {
            (pts[1], pts[0], pts[2], d02, d01, d12)
        } else {
            (pts[0], pts[1], pts[2], d12, d01, d02)
        };
        if l0 < 1e-3 || l1 < 1e-3 {
            return None;
        }
        let u0 = [(nose[0] - w0[0]) / l0, (nose[1] - w0[1]) / l0];
        let u1 = [(nose[0] - w1[0]) / l1, (nose[1] - w1[1]) / l1];
        let kx = u0[0] + u1[0];
        let ky = u0[1] + u1[1];
        if kx.hypot(ky) < 1e-3 {
            return None;
        }
        let angle_cw = wrap_angle_360(kx.atan2(-ky).to_degrees());
        let elongation = l0.min(l1) / short_d.max(1e-3);
        let symmetry = (1.0 - (l0 - l1).abs() / l0.max(l1)).max(0.0);
        let elong_score = ((elongation - 1.0) / 0.35).clamp(0.0, 1.0);
        let conf = ((0.65 + 0.18 * symmetry + 0.15 * elong_score) * conf_scale).clamp(0.65, 0.98);
        return Some((angle_cw, conf));
    }

    if contours.len() == 2 {
        let p0 = contour_bbox_center(&contours[0]);
        let p1 = contour_bbox_center(&contours[1]);
        let mid = [0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1])];
        let vx = img_center[0] - mid[0];
        let vy = img_center[1] - mid[1];
        if vx.hypot(vy) < 2.0 {
            return None;
        }
        let v0 = [p0[0] - img_center[0], p0[1] - img_center[1]];
        let v1 = [p1[0] - img_center[0], p1[1] - img_center[1]];
        let d0 = v0[0].hypot(v0[1]);
        let d1 = v1[0].hypot(v1[1]);
        let subtended_ok = if d0 >= 2.0 && d1 >= 2.0 {
            let cos_ang = ((v0[0] * v1[0] + v0[1] * v1[1]) / (d0 * d1)).clamp(-1.0, 1.0);
            let deg = cos_ang.acos().to_degrees();
            (35.0..=125.0).contains(&deg)
        } else {
            false
        };
        let angle_cw = wrap_angle_360(vx.atan2(-vy).to_degrees());
        let base_conf = if subtended_ok { 0.60 } else { 0.35 };
        return Some((angle_cw, base_conf * conf_scale));
    }

    None
}

pub fn extract_player_arrow_360(minimap_bgr: ArrayView3<'_, u8>) -> ArrowDetection {
    let (h, w, c) = minimap_bgr.dim();
    if (c != 3 && c != 4) || h.min(w) < 20 {
        return ArrowDetection::default();
    }
    let y0 = (0.4 * h as f64).round_ties_even() as usize;
    let y1 = (0.6 * h as f64).round_ties_even() as usize;
    let x0 = (0.4 * w as f64).round_ties_even() as usize;
    let x1 = (0.6 * w as f64).round_ties_even() as usize;
    if y1 <= y0 || x1 <= x0 || y1 > h || x1 > w {
        return ArrowDetection::default();
    }
    let crop = minimap_bgr.slice(s![y0..y1, x0..x1, 0..3]);
    let (ch, cw, _) = crop.dim();
    if ch.min(cw) < 4 {
        return ArrowDetection::default();
    }
    let Ok(up2) = resize_u8_hwc(crop, cw * 2, ch * 2, Interpolation::Linear) else {
        return ArrowDetection::default();
    };
    let (uh, uw, _) = up2.dim();
    let mut mask_bgr = Array2::<u8>::zeros((uh, uw));
    for y in 0..uh {
        for x in 0..uw {
            if up2[[y, x, 0]] > 240 && up2[[y, x, 1]] > 212 && up2[[y, x, 2]] <= 25 {
                mask_bgr[[y, x]] = 255;
            }
        }
    }

    let res_bgr = solve_arrow_from_mask(mask_bgr.view(), 1.0);
    if let Some((deg, conf)) = res_bgr
        && conf >= 0.65
    {
        return ArrowDetection {
            heading_deg: Some(deg),
            confidence: conf,
        };
    }

    let Ok(hsv) = bgr_to_hsv(up2.view()) else {
        return res_bgr.map_or_else(ArrowDetection::default, |(d, c)| ArrowDetection {
            heading_deg: Some(d),
            confidence: c,
        });
    };
    let Ok(mask_hsv) = in_range(hsv.view(), CYAN_HSV_LOWER, CYAN_HSV_UPPER) else {
        return res_bgr.map_or_else(ArrowDetection::default, |(d, c)| ArrowDetection {
            heading_deg: Some(d),
            confidence: c,
        });
    };
    let res_hsv = solve_arrow_from_mask(mask_hsv.view(), 0.92);
    if let Some((hd, hc)) = res_hsv
        && res_bgr.is_none_or(|(_, bc)| hc > bc)
    {
        return ArrowDetection {
            heading_deg: Some(hd),
            confidence: hc,
        };
    }
    if let Some((bd, bc)) = res_bgr {
        return ArrowDetection {
            heading_deg: Some(bd),
            confidence: bc,
        };
    }
    ArrowDetection::default()
}

pub fn extract_player_arrow_360_frame(frame: FrameView<'_>) -> ArrowDetection {
    let Some(arr) = frame_view_to_bgr_or_bgra(frame) else {
        return ArrowDetection::default();
    };
    extract_player_arrow_360(arr.view())
}

#[derive(Debug, Clone)]
pub struct CircularAngleSmoother {
    pub alpha: f64,
    pub deadband_deg: f64,
    sin: Option<f64>,
    cos: Option<f64>,
    wrapped_deg: Option<f64>,
    unwrapped_deg: Option<f64>,
}

impl Default for CircularAngleSmoother {
    fn default() -> Self {
        Self::new(0.35, 1.5).expect("default smoother params valid")
    }
}

impl CircularAngleSmoother {
    pub fn new(alpha: f64, deadband_deg: f64) -> CvResult<Self> {
        if !(alpha > 0.0 && alpha <= 1.0) {
            return Err(CvError::InvalidArgument(format!(
                "alpha must be in (0, 1], got {alpha}"
            )));
        }
        if deadband_deg < 0.0 || !deadband_deg.is_finite() {
            return Err(CvError::InvalidArgument(format!(
                "deadband_deg must be >= 0, got {deadband_deg}"
            )));
        }
        Ok(Self {
            alpha,
            deadband_deg,
            sin: None,
            cos: None,
            wrapped_deg: None,
            unwrapped_deg: None,
        })
    }

    pub fn reset(&mut self) {
        self.sin = None;
        self.cos = None;
        self.wrapped_deg = None;
        self.unwrapped_deg = None;
    }

    pub fn angle(&self) -> Option<f64> {
        self.wrapped_deg
    }

    pub fn unwrapped_angle(&self) -> Option<f64> {
        self.unwrapped_deg
    }

    pub fn update(&mut self, angle_deg: Option<f64>) -> Option<f64> {
        let Some(angle_in) = angle_deg else {
            return self.wrapped_deg;
        };
        let raw = wrap_angle_360(angle_in);
        let (Some(prev_w), Some(prev_s), Some(prev_c), Some(prev_u)) =
            (self.wrapped_deg, self.sin, self.cos, self.unwrapped_deg)
        else {
            let rad = raw.to_radians();
            self.sin = Some(rad.sin());
            self.cos = Some(rad.cos());
            self.wrapped_deg = Some(raw);
            self.unwrapped_deg = Some(raw);
            return self.wrapped_deg;
        };

        let delta = shortest_angle_delta(prev_w, raw);
        if delta.abs() < self.deadband_deg {
            return self.wrapped_deg;
        }

        let rad = raw.to_radians();
        let new_sin = (1.0 - self.alpha) * prev_s + self.alpha * rad.sin();
        let new_cos = (1.0 - self.alpha) * prev_c + self.alpha * rad.cos();
        let norm = new_sin.hypot(new_cos);
        let (s_out, c_out) = if norm < 1e-6 {
            let step_rad = wrap_angle_360(prev_w + self.alpha * delta).to_radians();
            (step_rad.sin(), step_rad.cos())
        } else {
            (new_sin / norm, new_cos / norm)
        };
        self.sin = Some(s_out);
        self.cos = Some(c_out);

        let new_wrapped = wrap_angle_360(s_out.atan2(c_out).to_degrees());
        let step = shortest_angle_delta(prev_w, new_wrapped);
        self.unwrapped_deg = Some(prev_u + step);
        self.wrapped_deg = Some(new_wrapped);
        self.wrapped_deg
    }
}

#[derive(Debug, Clone)]
pub struct CameraHeadingFuser {
    pub jump_deg: f64,
    pub confirm_frames: usize,
    pub confirm_window_s: f64,
    pub stale_s: f64,
    estimate: Option<f64>,
    conf: f64,
    source: FuserSource,
    last_accept_ts: Option<f64>,
    pending: Vec<(f64, f64)>,
}

impl Default for CameraHeadingFuser {
    fn default() -> Self {
        Self::new(50.0, 3, 0.6, 0.8)
    }
}

impl CameraHeadingFuser {
    pub fn new(jump_deg: f64, confirm_frames: usize, confirm_window_s: f64, stale_s: f64) -> Self {
        Self {
            jump_deg,
            confirm_frames: confirm_frames.max(1),
            confirm_window_s,
            stale_s,
            estimate: None,
            conf: 0.0,
            source: FuserSource::None,
            last_accept_ts: None,
            pending: Vec::new(),
        }
    }

    pub fn reset(&mut self) {
        self.estimate = None;
        self.conf = 0.0;
        self.source = FuserSource::None;
        self.last_accept_ts = None;
        self.pending.clear();
    }

    pub fn estimate(&self) -> Option<f64> {
        self.estimate
    }

    pub fn confidence(&self) -> f64 {
        self.conf
    }

    pub fn source(&self) -> FuserSource {
        self.source
    }

    fn accept(&mut self, angle: f64, conf: f64, ts: f64, source: FuserSource) {
        self.estimate = Some(wrap_angle_360(angle));
        self.conf = conf;
        self.source = source;
        self.last_accept_ts = Some(ts);
        self.pending.clear();
    }

    pub fn update(
        &mut self,
        cam_deg: Option<f64>,
        cam_conf: f64,
        method: ConeMethod,
        body_deg: Option<f64>,
        body_conf: f64,
        now: f64,
    ) -> Option<f64> {
        let trusted = cam_deg.is_some() && cam_conf > 0.0 && method.is_trusted();
        let guess = cam_deg.is_some() && cam_conf > 0.0 && !trusted;
        let body_ok = body_deg.is_some() && body_conf >= 0.5;

        if let Some(cd) = cam_deg
            && (trusted || guess)
        {
            let meas = wrap_angle_360(cd);
            let src = FuserSource::from(method);
            match self.estimate {
                None => {
                    if trusted {
                        self.accept(meas, cam_conf, now, src);
                    } else if let Some(bd) = body_deg
                        && body_ok
                        && shortest_angle_delta(bd, meas).abs() <= 50.0
                    {
                        self.accept(meas, cam_conf * 0.8, now, src);
                    } else if cam_conf >= 0.8 {
                        self.accept(meas, cam_conf * 0.7, now, src);
                    }
                }
                Some(est) => {
                    let jump = shortest_angle_delta(est, meas).abs();
                    let body_agrees = body_deg
                        .is_some_and(|bd| body_ok && shortest_angle_delta(bd, meas).abs() <= 50.0);
                    if jump <= (if trusted { self.jump_deg } else { 28.0 }) {
                        self.accept(meas, cam_conf, now, src);
                    } else if guess && body_agrees && jump <= 90.0 {
                        self.accept(meas, cam_conf * 0.8, now, src);
                    } else if guess && !body_agrees {
                        // Ignore uncorroborated tier-3 large jumps
                    } else {
                        self.pending.retain(|&(a, t)| {
                            (now - t) <= self.confirm_window_s
                                && shortest_angle_delta(a, meas).abs() <= 20.0
                        });
                        self.pending.push((meas, now));
                        let needed = if trusted {
                            self.confirm_frames
                        } else {
                            self.confirm_frames + 2
                        };
                        if self.pending.len() >= needed {
                            self.accept(meas, cam_conf, now, src);
                        }
                    }
                }
            }
        }

        let stale = self
            .last_accept_ts
            .is_none_or(|t| (now - t) > self.stale_s);
        if stale
            && body_ok
            && let Some(bd) = body_deg
        {
            let changed = self.estimate.is_none_or(|e| shortest_angle_delta(e, bd).abs() > 1.0);
            if changed || self.source != FuserSource::Body {
                self.estimate = Some(wrap_angle_360(bd));
                self.conf = (body_conf * 0.5).min(0.5);
                self.source = FuserSource::Body;
            }
        }
        self.estimate
    }
}

/// Pure-Rust OpenCV tracker ported from `src/tracker.py` (`CvAutoTrackBridge` & `HybridTracker` omitted).
#[derive(Debug, Clone)]
pub struct PureOpenCVTracker {
    pub cyan_lower: [u8; 3],
    pub cyan_upper: [u8; 3],
    pub yellow_lower: [u8; 3],
    pub yellow_upper: [u8; 3],
    pub last_arrow_confidence: f64,
    pub last_camera_confidence: f64,
    pub last_camera_method: ConeMethod,
}

impl Default for PureOpenCVTracker {
    fn default() -> Self {
        Self::new()
    }
}

impl PureOpenCVTracker {
    pub fn new() -> Self {
        Self {
            cyan_lower: CYAN_HSV_LOWER,
            cyan_upper: CYAN_HSV_UPPER,
            yellow_lower: YELLOW_PING_HSV_LOWER,
            yellow_upper: YELLOW_PING_HSV_UPPER,
            last_arrow_confidence: 0.0,
            last_camera_confidence: 0.0,
            last_camera_method: ConeMethod::None,
        }
    }

    pub fn find_player_on_fullscreen_map(
        &mut self,
        map_frame: ArrayView3<'_, u8>,
    ) -> Option<FullscreenPlayerPose> {
        let (h, w, c) = map_frame.dim();
        if h == 0 || w == 0 || c < 3 {
            return None;
        }
        let bgr = if c > 3 {
            map_frame.slice(s![.., .., 0..3])
        } else {
            map_frame
        };
        let hsv = bgr_to_hsv(bgr).ok()?;
        let mask = in_range(hsv.view(), self.cyan_lower, self.cyan_upper).ok()?;
        let mask = morphology_ex_u8(mask.view(), MorphOp::Open, morph_ellipse_3x3().view()).ok()?;
        let contours = find_contours_external(mask.view(), ContourMethod::Simple).ok()?;
        if contours.is_empty() {
            return None;
        }
        let mut best: Option<(&Vec<Point2i>, f64)> = None;
        for c in &contours {
            let area = contour_area(c);
            if area > 20.0 && area < 600.0 && best.is_none_or(|(_, ba)| area > ba) {
                best = Some((c, area));
            }
        }
        let (largest, _) = best?;
        let m = contour_moments(largest);
        if m.m00 == 0.0 {
            return None;
        }
        let cx = m.m10 / m.m00;
        let cy = m.m01 / m.m00;
        let mut best_tip = largest[0];
        let mut best_d2 = -1.0f64;
        for &p in largest {
            let dx = p.x as f64 - cx;
            let dy = p.y as f64 - cy;
            let d2 = dx * dx + dy * dy;
            if d2 > best_d2 {
                best_d2 = d2;
                best_tip = p;
            }
        }
        let angle = wrap_angle_360(
            (best_tip.x as f64 - cx)
                .atan2(-(best_tip.y as f64 - cy))
                .to_degrees(),
        );
        Some(FullscreenPlayerPose {
            pixel_x: cx,
            pixel_y: cy,
            angle_deg: angle,
        })
    }

    pub fn find_player_on_fullscreen_map_frame(
        &mut self,
        frame: FrameView<'_>,
    ) -> Option<FullscreenPlayerPose> {
        let arr = frame_view_to_bgr_or_bgra(frame)?;
        self.find_player_on_fullscreen_map(arr.view())
    }

    pub fn get_minimap_heading(&mut self, minimap_crop: ArrayView3<'_, u8>) -> Option<f64> {
        let det = extract_player_arrow_360(minimap_crop);
        self.last_arrow_confidence = det.confidence;
        det.heading_deg
    }

    pub fn get_minimap_camera_heading(&mut self, minimap_crop: ArrayView3<'_, u8>) -> Option<f64> {
        let det = extract_camera_view_cone(minimap_crop);
        self.last_camera_confidence = det.confidence;
        self.last_camera_method = det.method;
        det.heading_deg
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ops::morph::{draw_filled_convex_poly_u8, draw_filled_ellipse_sector_u8};

    fn make_synthetic_cone_bgr(size: usize, heading_cw_deg: f64, alpha_blend: f64) -> Array3<u8> {
        let mut base = Array3::<u8>::zeros((size, size, 3));
        for y in 0..size {
            for x in 0..size {
                let b = (45.0 + 18.0 * (x as f64 / 17.0).sin() + 14.0 * (y as f64 / 23.0).cos())
                    .clamp(10.0, 180.0);
                let g = (75.0 + 22.0 * (x as f64 / 13.0).cos() + 14.0 * (y as f64 / 19.0).sin())
                    .clamp(10.0, 180.0);
                let r = (55.0 + 16.0 * ((x + y) as f64 / 21.0).sin()).clamp(10.0, 180.0);
                base[[y, x, 0]] = b as u8;
                base[[y, x, 1]] = g as u8;
                base[[y, x, 2]] = r as u8;
            }
        }
        let center_cv_deg = heading_cw_deg - 90.0;
        let mut mask = Array2::<u8>::zeros((size, size));
        let c = size as f64 * 0.5;
        draw_filled_ellipse_sector_u8(
            &mut mask,
            c,
            c,
            size as f64 * 0.45,
            size as f64 * 0.45,
            center_cv_deg - 45.0,
            center_cv_deg + 45.0,
            255,
        );
        for y in 0..size {
            for x in 0..size {
                if mask[[y, x]] > 0 {
                    for ch in 0..3 {
                        let v = (1.0 - alpha_blend) * base[[y, x, ch]] as f64 + 255.0 * alpha_blend;
                        base[[y, x, ch]] = v.round().clamp(0.0, 255.0) as u8;
                    }
                }
            }
        }
        base
    }

    fn make_synthetic_cone_bgra(size: usize, heading_cw_deg: f64, inverted_alpha: bool) -> Array3<u8> {
        let mut img = Array3::<u8>::zeros((size, size, 4));
        for y in 0..size {
            for x in 0..size {
                img[[y, x, 0]] = 80;
                img[[y, x, 1]] = 110;
                img[[y, x, 2]] = 90;
                img[[y, x, 3]] = if inverted_alpha { 255 } else { 0 };
            }
        }
        let center_cv_deg = heading_cw_deg - 90.0;
        let mut alpha = Array2::<u8>::from_elem((size, size), if inverted_alpha { 255 } else { 0 });
        let c = size as f64 * 0.5;
        draw_filled_ellipse_sector_u8(
            &mut alpha,
            c,
            c,
            size as f64 * 0.45,
            size as f64 * 0.45,
            center_cv_deg - 45.0,
            center_cv_deg + 45.0,
            if inverted_alpha { 160 } else { 95 },
        );
        for y in 0..size {
            for x in 0..size {
                img[[y, x, 3]] = alpha[[y, x]];
            }
        }
        img
    }

    fn make_synthetic_arrow_bgr(
        size: usize,
        heading_cw_deg: f64,
        color_bgr: [u8; 3],
        occlude_nose: bool,
    ) -> Array3<u8> {
        let mut img = Array3::<u8>::from_elem((size, size, 3), 45u8);
        let c = size as f64 * 0.5;
        let su = size as f64 / 212.0;
        let rad = heading_cw_deg.to_radians();
        let fx = rad.sin();
        let fy = -rad.cos();
        let rx = rad.cos();
        let ry = rad.sin();

        let nose = Point2i {
            x: (c + 13.0 * su * fx).round() as i32,
            y: (c + 13.0 * su * fy).round() as i32,
        };
        let w_left = Point2i {
            x: (c - 9.0 * su * fx - 8.5 * su * rx).round() as i32,
            y: (c - 9.0 * su * fy - 8.5 * su * ry).round() as i32,
        };
        let notch = Point2i {
            x: (c - 4.0 * su * fx).round() as i32,
            y: (c - 4.0 * su * fy).round() as i32,
        };
        let w_right = Point2i {
            x: (c - 9.0 * su * fx + 8.5 * su * rx).round() as i32,
            y: (c - 9.0 * su * fy + 8.5 * su * ry).round() as i32,
        };

        let mut mask = Array2::<u8>::zeros((size, size));
        draw_filled_convex_poly_u8(&mut mask, &[nose, w_left, notch], 255);
        draw_filled_convex_poly_u8(&mut mask, &[nose, notch, w_right], 255);
        if occlude_nose {
            let occ_cx = (c + 10.5 * su * fx).round() as i32;
            let occ_cy = (c + 10.5 * su * fy).round() as i32;
            draw_filled_circle_u8(&mut mask, occ_cx, occ_cy, (4.5 * su).round() as i32, 0);
        }
        for y in 0..size {
            for x in 0..size {
                if mask[[y, x]] > 0 {
                    img[[y, x, 0]] = color_bgr[0];
                    img[[y, x, 1]] = color_bgr[1];
                    img[[y, x, 2]] = color_bgr[2];
                }
            }
        }
        img
    }

    #[test]
    fn heading360_angle_helpers_and_nav_relative() {
        assert_eq!(wrap_angle_360(-90.0), 270.0);
        assert_eq!(wrap_angle_360(360.0), 0.0);
        assert_eq!(wrap_angle_360(-0.0), 0.0);
        assert!((shortest_angle_delta(350.0, 10.0) - 20.0).abs() < 1e-9);
        assert!((shortest_angle_delta(10.0, 350.0) + 20.0).abs() < 1e-9);
        assert_eq!(shortest_angle_delta(0.0, 180.0), 180.0);
        assert_eq!(shortest_angle_delta(0.0, 360.0), 0.0);

        let cases = [
            ([0.0, 0.0], [0.0, -100.0], 0.0, 0.0, 100.0),
            ([0.0, 0.0], [100.0, 0.0], 0.0, 90.0, 100.0),
            ([0.0, 0.0], [-100.0, 0.0], 0.0, -90.0, 100.0),
            ([0.0, 0.0], [0.0, 100.0], 0.0, 180.0, 100.0),
            ([100.0, -100.0], [100.0, -100.0], 45.0, 0.0, 0.0),
            ([0.0, 0.0], [100.0, -100.0], 45.0, 0.0, 20_000.0f64.sqrt()),
            ([0.0, 0.0], [0.0, -100.0], 350.0, 10.0, 100.0),
            ([0.0, 0.0], [-100.0, -100.0], 350.0, -35.0, 20_000.0f64.sqrt()),
            ([10.0, 20.0], [10.0, 20.0], 123.0, 0.0, 0.0),
        ];
        for (p, t, v, exp_d, exp_dist) in cases {
            let (d, dist) = compute_nav_relative_angle(p, t, v);
            assert!((d - exp_d).abs() < 1e-6, "got {d} exp {exp_d}");
            assert!((dist - exp_dist).abs() < 1e-6);
        }
    }

    #[test]
    fn circular_angle_smoother_355_to_5_and_reverse() {
        let mut sm = CircularAngleSmoother::new(0.35, 1.5).unwrap();
        assert_eq!(sm.update(Some(355.0)), Some(355.0));
        // Inside deadband
        assert_eq!(sm.update(Some(355.8)), Some(355.0));
        // None holds previous
        assert_eq!(sm.update(None), Some(355.0));
        // Step across 0/360 forward
        for _ in 0..8 {
            sm.update(Some(5.0));
        }
        assert!(sm.unwrapped_angle().unwrap() > 363.0);

        // Reverse across 0/360
        sm.reset();
        sm.update(Some(5.0));
        for _ in 0..8 {
            sm.update(Some(355.0));
        }
        assert!(sm.unwrapped_angle().unwrap() < -3.0);

        // 180-degree cancellation guard
        let mut s2 = CircularAngleSmoother::new(0.5, 0.0).unwrap();
        s2.update(Some(0.0));
        let out = s2.update(Some(180.0)).unwrap();
        assert!(out.is_finite());
    }

    #[test]
    fn camera_heading_fuser_tier3_rejection_and_body_fallback() {
        let mut fuser = CameraHeadingFuser::default();
        // Establish trusted BgraAlpha lock at 42 deg
        let est = fuser.update(Some(42.0), 0.92, ConeMethod::BgraAlpha, Some(40.0), 0.9, 1.0);
        assert_eq!(est, Some(42.0));
        assert_eq!(fuser.source(), FuserSource::BgraAlpha);

        // False 177 deg Tier-3 guess while body is still 40 deg must be rejected
        for i in 1..=6 {
            let e = fuser.update(
                Some(177.0),
                0.70,
                ConeMethod::HsvAdaptive,
                Some(40.0),
                0.9,
                1.0 + i as f64 * 0.05,
            );
            assert!((e.unwrap() - 42.0).abs() < 1e-6);
            assert_eq!(fuser.source(), FuserSource::BgraAlpha);
        }

        // Single-frame trusted jump 250 deg rejected; 3-frame confirmation accepted
        let e_single = fuser.update(Some(250.0), 0.9, ConeMethod::BgrBlend, Some(40.0), 0.9, 1.4);
        assert!((e_single.unwrap() - 42.0).abs() < 1e-6);
        fuser.update(Some(251.0), 0.9, ConeMethod::BgrBlend, Some(40.0), 0.9, 1.45);
        let e_conf = fuser.update(Some(249.0), 0.9, ConeMethod::BgrBlend, Some(40.0), 0.9, 1.50);
        assert!((e_conf.unwrap() - 249.0).abs() < 1e-6);

        // Stale > 0.8s falls back to Body
        let e_stale = fuser.update(None, 0.0, ConeMethod::None, Some(120.0), 0.9, 2.5);
        assert_eq!(e_stale, Some(120.0));
        assert_eq!(fuser.source(), FuserSource::Body);
        assert!(fuser.confidence() <= 0.5);

        // Agreeing Tier-3 within 50 deg of body re-accepted
        let e_agree = fuser.update(
            Some(130.0),
            0.70,
            ConeMethod::HsvAdaptive,
            Some(120.0),
            0.9,
            2.6,
        );
        assert!((e_agree.unwrap() - 130.0).abs() < 1e-6);
        assert_eq!(fuser.source(), FuserSource::HsvAdaptive);
    }

    #[test]
    fn synthetic_cones_and_arrows_parity() {
        // 8 compass directions for BGRA normal & inverted alpha, plus BGR 0.25 blend
        for deg in [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0] {
            let bgra = make_synthetic_cone_bgra(212, deg, false);
            let det = extract_camera_view_cone(bgra.view());
            assert_eq!(det.method, ConeMethod::BgraAlpha);
            assert!(
                shortest_angle_delta(deg, det.heading_deg.unwrap()).abs() < 1.5,
                "bgra deg={deg} got {:?}",
                det.heading_deg
            );

            let inv = make_synthetic_cone_bgra(212, deg, true);
            let det_inv = extract_camera_view_cone(inv.view());
            assert_eq!(det_inv.method, ConeMethod::BgraAlpha);
            assert!(
                shortest_angle_delta(deg, det_inv.heading_deg.unwrap()).abs() < 1.5,
                "bgra inv deg={deg} got {:?}",
                det_inv.heading_deg
            );

            let bgr = make_synthetic_cone_bgr(212, deg, 0.25);
            let det_bgr = extract_camera_view_cone(bgr.view());
            assert!(det_bgr.method.is_trusted());
            assert!(
                shortest_angle_delta(deg, det_bgr.heading_deg.unwrap()).abs() < 2.5,
                "bgr deg={deg} got {:?}",
                det_bgr.heading_deg
            );
        }

        // Dimmed 0.14 cone
        let dim = make_synthetic_cone_bgr(212, 135.0, 0.14);
        let det_dim = extract_camera_view_cone(dim.view());
        assert!(det_dim.heading_deg.is_some());
        assert!(shortest_angle_delta(135.0, det_dim.heading_deg.unwrap()).abs() < 3.0);

        // Flat snow -> None
        let snow = Array3::<u8>::from_elem((212, 212, 3), 245u8);
        assert_eq!(extract_camera_view_cone(snow.view()).heading_deg, None);

        // 3-tip cyan arrow + FSR2 dimmed HSV + occluded-nose 2-wing + 720p/4K
        for deg in [0.0, 90.0, 180.0, 270.0] {
            let arrow = make_synthetic_arrow_bgr(212, deg, [255, 240, 0], false);
            let ad = extract_player_arrow_360(arrow.view());
            assert!(
                ad.heading_deg.is_some() && shortest_angle_delta(deg, ad.heading_deg.unwrap()).abs() < 2.5,
                "3-tip deg={deg} got {:?}",
                ad
            );

            let fsr = make_synthetic_arrow_bgr(212, deg, [222, 198, 30], false);
            let af = extract_player_arrow_360(fsr.view());
            assert!(
                af.heading_deg.is_some() && shortest_angle_delta(deg, af.heading_deg.unwrap()).abs() < 3.5,
                "fsr deg={deg} got {:?}",
                af
            );
        }

        // Occluded nose 2-wing fallback
        let occ = make_synthetic_arrow_bgr(212, 60.0, [255, 240, 0], true);
        let ao = extract_player_arrow_360(occ.view());
        assert!(
            ao.heading_deg.is_some() && shortest_angle_delta(60.0, ao.heading_deg.unwrap()).abs() < 12.0,
            "occluded nose got {:?}",
            ao
        );

        // 720p (140px) & 4K (424px)
        for sz in [140usize, 424] {
            let a_sz = make_synthetic_arrow_bgr(sz, 135.0, [255, 240, 0], false);
            let det = extract_player_arrow_360(a_sz.view());
            assert!(
                det.heading_deg.is_some() && shortest_angle_delta(135.0, det.heading_deg.unwrap()).abs() < 3.5,
                "sz={sz} got {:?}",
                det
            );
        }

        // Arrow-free -> None
        let empty = Array3::<u8>::from_elem((212, 212, 3), 45u8);
        assert_eq!(extract_player_arrow_360(empty.view()).heading_deg, None);

        // PureOpenCVTracker fullscreen map arrow
        let mut tracker = PureOpenCVTracker::new();
        let full_arrow = make_synthetic_arrow_bgr(212, 90.0, [255, 240, 0], false);
        let pose = tracker
            .find_player_on_fullscreen_map(full_arrow.view())
            .expect("fullscreen pose");
        assert!(shortest_angle_delta(90.0, pose.angle_deg).abs() < 15.0);
    }

    #[test]
    fn player_arrow_bgr_threshold_includes_r_25() {
        // Background [245, 215, 26] and arrow [245, 215, 25] both fall inside CYAN_HSV,
        // so HSV fallback floods the entire crop (`res_hsv = None`). Only `R <= 25` in the
        // primary BGR mask isolates the arrow; `R < 25` would yield `None`.
        let mut img = Array3::<u8>::zeros((212, 212, 3));
        for y in 0..212 {
            for x in 0..212 {
                img[[y, x, 0]] = 245;
                img[[y, x, 1]] = 215;
                img[[y, x, 2]] = 26;
            }
        }
        let arrow = make_synthetic_arrow_bgr(212, 45.0, [245, 215, 25], false);
        for y in 0..212 {
            for x in 0..212 {
                if arrow[[y, x, 0]] == 245 && arrow[[y, x, 1]] == 215 && arrow[[y, x, 2]] == 25 {
                    img[[y, x, 2]] = 25;
                }
            }
        }
        let det = extract_player_arrow_360(img.view());
        let deg = det
            .heading_deg
            .expect("R == 25 arrow must match cv2.threshold(r, 25, 255, THRESH_BINARY_INV)");
        assert!(
            shortest_angle_delta(45.0, deg).abs() < 2.5,
            "expected ~45.0 deg, got {deg}"
        );
        assert!(det.confidence >= 0.65, "conf={}", det.confidence);
    }
}
