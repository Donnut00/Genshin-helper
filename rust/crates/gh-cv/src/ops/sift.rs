//! DoG Scale-Space SIFT (`SiftExtractor` implementing `FeatureMatcher`),
//! RootSIFT (`L1 + sqrt`) and quantized `u8` helpers.

use crate::ops::color::frame_view_to_gray_array;
use crate::ops::filters::gaussian_blur_f32;
use crate::ops::resize::resize_f32;
use crate::ops::{CvError, CvResult, FeatureMatcher, Interpolation, KeyPoint};
use gh_core::frame::FrameView;
use ndarray::{Array2, ArrayView2};

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SiftParams {
    pub nfeatures: usize,
    pub n_octave_layers: usize,
    pub contrast_threshold: f64,
    pub edge_threshold: f64,
    pub sigma: f64,
}

impl Default for SiftParams {
    fn default() -> Self {
        Self {
            nfeatures: 0,
            n_octave_layers: 3,
            contrast_threshold: 0.04,
            edge_threshold: 10.0,
            sigma: 1.6,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SiftExtractor {
    pub params: SiftParams,
}

impl SiftExtractor {
    pub fn new(
        nfeatures: usize,
        contrast_threshold: f64,
        edge_threshold: f64,
    ) -> Self {
        Self {
            params: SiftParams {
                nfeatures,
                n_octave_layers: 3,
                contrast_threshold,
                edge_threshold,
                sigma: 1.6,
            },
        }
    }

    pub fn detect_and_compute(
        &self,
        gray: ArrayView2<'_, u8>,
        mask: Option<ArrayView2<'_, u8>>,
    ) -> CvResult<(Vec<KeyPoint>, Vec<[f32; 128]>)> {
        let (h, w) = gray.dim();
        if h < 4 || w < 4 {
            return Ok((Vec::new(), Vec::new()));
        }
        if let Some(m) = mask
            && m.dim() != (h, w)
        {
            return Err(CvError::ShapeMismatch("SIFT mask dimension mismatch".into()));
        }

        // Convert to [0, 1] float32 and build -1 octave base image (2x upscaled, sigma=1.6)
        let base_0 = gray.mapv(|v| v as f32 * (1.0 / 255.0));
        let up2 = resize_f32(base_0.view(), w * 2, h * 2, Interpolation::Linear)?;
        let sig_diff = (self.params.sigma * self.params.sigma - 1.0).max(0.01).sqrt();
        let k_blur = ((sig_diff * 6.0).ceil() as usize | 1).max(3);
        let init_img = gaussian_blur_f32(up2.view(), k_blur, k_blur, sig_diff, sig_diff)?;

        let min_dim = (w * 2).min(h * 2);
        let max_octaves = ((min_dim as f64).log2().floor() as isize - 2).clamp(1, 7) as usize;
        let n_oct_layers = self.params.n_octave_layers.max(1);
        let n_gauss_layers = n_oct_layers + 3;

        // Incremental blur sigmas matching Lowe / OpenCV
        let k = 2.0f64.powf(1.0 / n_oct_layers as f64);
        let mut sigmas = vec![0.0f64; n_gauss_layers];
        sigmas[0] = self.params.sigma;
        for (i, slot) in sigmas.iter_mut().enumerate().skip(1) {
            let sig_prev = self.params.sigma * k.powi((i - 1) as i32);
            let sig_total = sig_prev * k;
            *slot = (sig_total * sig_total - sig_prev * sig_prev).max(1e-6).sqrt();
        }

        let mut gauss_pyr: Vec<Vec<Array2<f32>>> = Vec::with_capacity(max_octaves);
        for o in 0..max_octaves {
            let mut oct = Vec::with_capacity(n_gauss_layers);
            if o == 0 {
                oct.push(init_img.clone());
            } else {
                let prev = &gauss_pyr[o - 1][n_oct_layers];
                let (ph, pw) = prev.dim();
                let nh = ph / 2;
                let nw = pw / 2;
                if nh < 8 || nw < 8 {
                    break;
                }
                let mut down = Array2::<f32>::zeros((nh, nw));
                for y in 0..nh {
                    for x in 0..nw {
                        down[[y, x]] = prev[[y * 2, x * 2]];
                    }
                }
                oct.push(down);
            }
            for &sig in sigmas.iter().skip(1) {
                let ks = ((sig * 6.0).ceil() as usize | 1).max(3);
                let prev_img = oct.last().unwrap();
                let blurred = gaussian_blur_f32(prev_img.view(), ks, ks, sig, sig)?;
                oct.push(blurred);
            }
            gauss_pyr.push(oct);
        }

        // Build DoG pyramid
        let mut dog_pyr: Vec<Vec<Array2<f32>>> = Vec::with_capacity(gauss_pyr.len());
        for oct in &gauss_pyr {
            let mut dogs = Vec::with_capacity(oct.len() - 1);
            for i in 0..(oct.len() - 1) {
                let a = &oct[i + 1];
                let b = &oct[i];
                let mut d = Array2::<f32>::zeros(a.dim());
                for ((dst, &va), &vb) in d.iter_mut().zip(a.iter()).zip(b.iter()) {
                    *dst = va - vb;
                }
                dogs.push(d);
            }
            dog_pyr.push(dogs);
        }

        let prelim_thresh = (0.5 * self.params.contrast_threshold / n_oct_layers as f64) as f32;
        let edge_r = self.params.edge_threshold;
        let edge_thresh_ratio = ((edge_r + 1.0) * (edge_r + 1.0)) / edge_r;
        const BORDER: usize = 5;
        const SIFT_MAX_INTERP_STEPS: usize = 5;

        let mut keypoints = Vec::new();
        let mut descriptors = Vec::new();

        for (o, dogs) in dog_pyr.iter().enumerate() {
            let oct_scale = 0.5f64 * (1usize << o) as f64;
            let (oh, ow) = dogs[0].dim();
            if oh <= BORDER * 2 || ow <= BORDER * 2 {
                continue;
            }
            for layer_init in 1..=n_oct_layers {
                let prev = &dogs[layer_init - 1];
                let curr = &dogs[layer_init];
                let next = &dogs[layer_init + 1];
                for y_init in BORDER..(oh - BORDER) {
                    for x_init in BORDER..(ow - BORDER) {
                        let val = curr[[y_init, x_init]];
                        if val.abs() <= prelim_thresh {
                            continue;
                        }
                        if !is_extremum_26(prev, curr, next, y_init, x_init, val) {
                            continue;
                        }

                        // Subpixel 3D Taylor refinement
                        let mut x = x_init as isize;
                        let mut y = y_init as isize;
                        let mut layer = layer_init as isize;
                        let mut converged = false;
                        let mut offset = [0.0f64; 3];
                        for _ in 0..SIFT_MAX_INTERP_STEPS {
                            if x < BORDER as isize
                                || x >= (ow - BORDER) as isize
                                || y < BORDER as isize
                                || y >= (oh - BORDER) as isize
                                || layer < 1
                                || layer > n_oct_layers as isize
                            {
                                break;
                            }
                            let ux = x as usize;
                            let uy = y as usize;
                            let ul = layer as usize;
                            let dp = &dogs[ul - 1];
                            let dc = &dogs[ul];
                            let dn = &dogs[ul + 1];

                            let dx = 0.5 * (dc[[uy, ux + 1]] - dc[[uy, ux - 1]]) as f64;
                            let dy = 0.5 * (dc[[uy + 1, ux]] - dc[[uy - 1, ux]]) as f64;
                            let ds = 0.5 * (dn[[uy, ux]] - dp[[uy, ux]]) as f64;
                            let v2 = 2.0 * dc[[uy, ux]] as f64;
                            let dxx = dc[[uy, ux + 1]] as f64 + dc[[uy, ux - 1]] as f64 - v2;
                            let dyy = dc[[uy + 1, ux]] as f64 + dc[[uy - 1, ux]] as f64 - v2;
                            let dss = dn[[uy, ux]] as f64 + dp[[uy, ux]] as f64 - v2;
                            let dxy = 0.25
                                * (dc[[uy + 1, ux + 1]] as f64
                                    - dc[[uy + 1, ux - 1]] as f64
                                    - dc[[uy - 1, ux + 1]] as f64
                                    + dc[[uy - 1, ux - 1]] as f64);
                            let dxs = 0.25
                                * (dn[[uy, ux + 1]] as f64
                                    - dn[[uy, ux - 1]] as f64
                                    - dp[[uy, ux + 1]] as f64
                                    + dp[[uy, ux - 1]] as f64);
                            let dys = 0.25
                                * (dn[[uy + 1, ux]] as f64
                                    - dn[[uy - 1, ux]] as f64
                                    - dp[[uy + 1, ux]] as f64
                                    + dp[[uy - 1, ux]] as f64);

                            let h_mat = [
                                [dxx, dxy, dxs],
                                [dxy, dyy, dys],
                                [dxs, dys, dss],
                            ];
                            let rhs = [-dx, -dy, -ds];
                            let Some(sol) = solve_3x3(&h_mat, &rhs) else {
                                break;
                            };
                            offset = sol;
                            if offset[0].abs() < 0.5
                                && offset[1].abs() < 0.5
                                && offset[2].abs() < 0.5
                            {
                                converged = true;
                                break;
                            }
                            if !offset[0].is_finite()
                                || !offset[1].is_finite()
                                || !offset[2].is_finite()
                                || offset[0].abs() > 1e4
                                || offset[1].abs() > 1e4
                                || offset[2].abs() > 1e4
                            {
                                break;
                            }
                            x += offset[0].round() as isize;
                            y += offset[1].round() as isize;
                            layer += offset[2].round() as isize;
                        }
                        if !converged {
                            continue;
                        }
                        if x < BORDER as isize
                            || x >= (ow - BORDER) as isize
                            || y < BORDER as isize
                            || y >= (oh - BORDER) as isize
                            || layer < 1
                            || layer > n_oct_layers as isize
                        {
                            continue;
                        }
                        let ux = x as usize;
                        let uy = y as usize;
                        let ul = layer as usize;
                        let dp = &dogs[ul - 1];
                        let dc = &dogs[ul];
                        let dn = &dogs[ul + 1];

                        let dx = 0.5 * (dc[[uy, ux + 1]] - dc[[uy, ux - 1]]) as f64;
                        let dy = 0.5 * (dc[[uy + 1, ux]] - dc[[uy - 1, ux]]) as f64;
                        let ds = 0.5 * (dn[[uy, ux]] - dp[[uy, ux]]) as f64;
                        let contr = dc[[uy, ux]] as f64
                            + 0.5 * (dx * offset[0] + dy * offset[1] + ds * offset[2]);
                        if contr.abs() * (n_oct_layers as f64) < self.params.contrast_threshold {
                            continue;
                        }

                        // Edge response check
                        let v2 = 2.0 * dc[[uy, ux]] as f64;
                        let dxx = dc[[uy, ux + 1]] as f64 + dc[[uy, ux - 1]] as f64 - v2;
                        let dyy = dc[[uy + 1, ux]] as f64 + dc[[uy - 1, ux]] as f64 - v2;
                        let dxy = 0.25
                            * (dc[[uy + 1, ux + 1]] as f64
                                - dc[[uy + 1, ux - 1]] as f64
                                - dc[[uy - 1, ux + 1]] as f64
                                + dc[[uy - 1, ux - 1]] as f64);
                        let tr = dxx + dyy;
                        let det = dxx * dyy - dxy * dxy;
                        if det <= 0.0 || tr * tr >= edge_thresh_ratio * det {
                            continue;
                        }

                        let fx_img = (x as f64 + offset[0]) * oct_scale;
                        let fy_img = (y as f64 + offset[1]) * oct_scale;
                        if let Some(m) = mask {
                            let mx = (fx_img.round() as isize).clamp(0, w as isize - 1) as usize;
                            let my = (fy_img.round() as isize).clamp(0, h as isize - 1) as usize;
                            if m[[my, mx]] == 0 {
                                continue;
                            }
                        }

                        let scale_in_oct = self.params.sigma
                            * 2.0f64.powf((layer as f64 + offset[2]) / n_oct_layers as f64);
                        let kpt_size = (scale_in_oct * oct_scale * 2.0) as f32;
                        let gauss_img = &gauss_pyr[o][ul];

                        // Orientation histogram (36 bins)
                        let hist = calc_orientation_hist(
                            gauss_img.view(),
                            ux,
                            uy,
                            (3.0 * 1.5 * scale_in_oct).round() as isize,
                            1.5 * scale_in_oct,
                        );
                        let max_h = hist.iter().copied().fold(0.0f64, f64::max);
                        if max_h <= 0.0 {
                            continue;
                        }
                        let mag_thr = max_h * 0.8;
                        for b in 0..36 {
                            let prev_b = (b + 35) % 36;
                            let next_b = (b + 1) % 36;
                            if hist[b] > hist[prev_b]
                                && hist[b] > hist[next_b]
                                && hist[b] >= mag_thr
                            {
                                let denom = hist[prev_b] - 2.0 * hist[b] + hist[next_b];
                                let sub = if denom.abs() > 1e-12 {
                                    0.5 * (hist[prev_b] - hist[next_b]) / denom
                                } else {
                                    0.0
                                };
                                let mut bin = b as f64 + sub;
                                if bin < 0.0 {
                                    bin += 36.0;
                                } else if bin >= 36.0 {
                                    bin -= 36.0;
                                }
                                let angle_deg = (360.0 - bin * 10.0) % 360.0;
                                let desc = calc_sift_descriptor(
                                    gauss_img.view(),
                                    x as f64 + offset[0],
                                    y as f64 + offset[1],
                                    angle_deg,
                                    scale_in_oct,
                                );
                                keypoints.push(KeyPoint {
                                    x: fx_img as f32,
                                    y: fy_img as f32,
                                    size: kpt_size,
                                    angle: angle_deg as f32,
                                    response: contr.abs() as f32,
                                    octave: o as i32 - 1,
                                });
                                descriptors.push(desc);
                            }
                        }
                    }
                }
            }
        }

        // Retain top nfeatures by response if requested
        if self.params.nfeatures > 0 && keypoints.len() > self.params.nfeatures {
            let mut order: Vec<usize> = (0..keypoints.len()).collect();
            order.sort_by(|&a, &b| keypoints[b].response.total_cmp(&keypoints[a].response));
            order.truncate(self.params.nfeatures);
            let kps_out = order.iter().map(|&i| keypoints[i]).collect();
            let des_out = order.iter().map(|&i| descriptors[i]).collect();
            return Ok((kps_out, des_out));
        }

        Ok((keypoints, descriptors))
    }
}

impl FeatureMatcher for SiftExtractor {
    fn extract_keypoints_and_descriptors(&self, gray: FrameView<'_>) -> Vec<([f32; 2], Vec<f32>)> {
        let Ok(arr) = frame_view_to_gray_array(gray) else {
            return Vec::new();
        };
        let Ok((kps, des)) = self.detect_and_compute(arr.view(), None) else {
            return Vec::new();
        };
        kps.into_iter()
            .zip(des)
            .map(|(k, d)| ([k.x, k.y], d.to_vec()))
            .collect()
    }
}

#[inline]
fn is_extremum_26(
    prev: &Array2<f32>,
    curr: &Array2<f32>,
    next: &Array2<f32>,
    y: usize,
    x: usize,
    val: f32,
) -> bool {
    if val > 0.0 {
        for dy in [y - 1, y, y + 1] {
            for dx in [x - 1, x, x + 1] {
                if val <= prev[[dy, dx]] || val <= next[[dy, dx]] {
                    return false;
                }
                if (dy != y || dx != x) && val <= curr[[dy, dx]] {
                    return false;
                }
            }
        }
        true
    } else {
        for dy in [y - 1, y, y + 1] {
            for dx in [x - 1, x, x + 1] {
                if val >= prev[[dy, dx]] || val >= next[[dy, dx]] {
                    return false;
                }
                if (dy != y || dx != x) && val >= curr[[dy, dx]] {
                    return false;
                }
            }
        }
        true
    }
}

fn solve_3x3(a: &[[f64; 3]; 3], b: &[f64; 3]) -> Option<[f64; 3]> {
    let det = a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
    if det.abs() <= 1e-14 {
        return None;
    }
    let inv = 1.0 / det;
    let x0 = (b[0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (b[1] * a[2][2] - a[1][2] * b[2])
        + a[0][2] * (b[1] * a[2][1] - a[1][1] * b[2]))
        * inv;
    let x1 = (a[0][0] * (b[1] * a[2][2] - a[1][2] * b[2])
        - b[0] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * b[2] - b[1] * a[2][0]))
        * inv;
    let x2 = (a[0][0] * (a[1][1] * b[2] - b[1] * a[2][1])
        - a[0][1] * (a[1][0] * b[2] - b[1] * a[2][0])
        + b[0] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
        * inv;
    Some([x0, x1, x2])
}

fn calc_orientation_hist(
    img: ArrayView2<'_, f32>,
    cx: usize,
    cy: usize,
    radius: isize,
    sigma: f64,
) -> [f64; 36] {
    let (h, w) = img.dim();
    let mut raw = [0.0f64; 36];
    let exp_scale = -1.0 / (2.0 * sigma * sigma);
    let r = radius.clamp(1, 48);
    for dy in -r..=r {
        let py = cy as isize + dy;
        if py <= 0 || py >= h as isize - 1 {
            continue;
        }
        let uy = py as usize;
        for dx in -r..=r {
            let px = cx as isize + dx;
            if px <= 0 || px >= w as isize - 1 {
                continue;
            }
            let ux = px as usize;
            let gx = (img[[uy, ux + 1]] - img[[uy, ux - 1]]) as f64;
            let gy = (img[[uy - 1, ux]] - img[[uy + 1, ux]]) as f64;
            let mag = gx.hypot(gy);
            let wt = ((dx * dx + dy * dy) as f64 * exp_scale).exp();
            let mut ori = gy.atan2(gx).to_degrees();
            if ori < 0.0 {
                ori += 360.0;
            }
            let bin = ((ori * 0.1).round() as usize) % 36;
            raw[bin] += wt * mag;
        }
    }
    let mut smoothed = [0.0f64; 36];
    for i in 0..36 {
        let im2 = (i + 34) % 36;
        let im1 = (i + 35) % 36;
        let ip1 = (i + 1) % 36;
        let ip2 = (i + 2) % 36;
        smoothed[i] =
            (raw[im2] + raw[ip2]) / 16.0 + (raw[im1] + raw[ip1]) * 4.0 / 16.0 + raw[i] * 6.0 / 16.0;
    }
    smoothed
}

fn calc_sift_descriptor(
    img: ArrayView2<'_, f32>,
    ptx: f64,
    pty: f64,
    ori_deg: f64,
    scl: f64,
) -> [f32; 128] {
    const D: usize = 4;
    const N_ORI: usize = 8;
    let (h, w) = img.dim();
    let angle_rad = (360.0 - ori_deg).to_radians();
    let cos_t = angle_rad.cos();
    let sin_t = angle_rad.sin();
    let hist_width = 3.0 * scl;
    let cos_rot = cos_t / hist_width;
    let sin_rot = sin_t / hist_width;
    let exp_scale = -1.0 / (0.5 * (D as f64) * (D as f64));
    let radius = ((hist_width * std::f64::consts::SQRT_2 * (D as f64 + 1.0) * 0.5).round() as isize)
        .min((h.max(w) / 2) as isize);

    let cx = ptx.round() as isize;
    let cy = pty.round() as isize;
    let mut hist = [0.0f64; D * D * N_ORI];

    for i in -radius..=radius {
        let py = cy + i;
        if py <= 0 || py >= h as isize - 1 {
            continue;
        }
        let uy = py as usize;
        for j in -radius..=radius {
            let px = cx + j;
            if px <= 0 || px >= w as isize - 1 {
                continue;
            }
            let ux = px as usize;
            let c_rot = (j as f64) * cos_rot - (i as f64) * sin_rot;
            let r_rot = (j as f64) * sin_rot + (i as f64) * cos_rot;
            let rbin = r_rot + (D as f64) * 0.5 - 0.5;
            let cbin = c_rot + (D as f64) * 0.5 - 0.5;
            if rbin <= -1.0 || rbin >= D as f64 || cbin <= -1.0 || cbin >= D as f64 {
                continue;
            }
            let gx = (img[[uy, ux + 1]] - img[[uy, ux - 1]]) as f64;
            let gy = (img[[uy - 1, ux]] - img[[uy + 1, ux]]) as f64;
            let mag = gx.hypot(gy);
            let wt = ((c_rot * c_rot + r_rot * r_rot) * exp_scale).exp() * mag;

            let mut ori = gy.atan2(gx).to_degrees();
            if ori < 0.0 {
                ori += 360.0;
            }
            let mut obin = (ori - (360.0 - ori_deg)) * (N_ORI as f64 / 360.0);
            obin = obin.rem_euclid(N_ORI as f64);

            let r0 = rbin.floor() as isize;
            let c0 = cbin.floor() as isize;
            let o0 = obin.floor() as usize;
            let dr = rbin - r0 as f64;
            let dc = cbin - c0 as f64;
            let do_f = obin - o0 as f64;

            for dr_i in 0..2isize {
                let rr = r0 + dr_i;
                if rr < 0 || rr >= D as isize {
                    continue;
                }
                let wr = if dr_i == 0 { 1.0 - dr } else { dr };
                for dc_i in 0..2isize {
                    let cc = c0 + dc_i;
                    if cc < 0 || cc >= D as isize {
                        continue;
                    }
                    let wc = if dc_i == 0 { 1.0 - dc } else { dc };
                    for do_i in 0..2usize {
                        let oo = (o0 + do_i) % N_ORI;
                        let wo = if do_i == 0 { 1.0 - do_f } else { do_f };
                        let idx = ((rr as usize) * D + (cc as usize)) * N_ORI + oo;
                        hist[idx] += wt * wr * wc * wo;
                    }
                }
            }
        }
    }

    // L2 normalize, clamp <= 0.2, re-normalize, scale by 512 matching OpenCV SIFT
    let norm1 = hist.iter().map(|&v| v * v).sum::<f64>().sqrt();
    let thr = norm1 * 0.2;
    let mut norm2_sq = 0.0f64;
    for v in &mut hist {
        if *v > thr {
            *v = thr;
        }
        norm2_sq += *v * *v;
    }
    let scale = if norm2_sq > 1e-18 {
        512.0 / norm2_sq.sqrt()
    } else {
        0.0
    };
    let mut out = [0.0f32; 128];
    for (dst, &v) in out.iter_mut().zip(hist.iter()) {
        *dst = (v * scale) as f32;
    }
    out
}

/// Converts a raw SIFT descriptor to RootSIFT (`L1`-normalize then elementwise `sqrt`).
pub fn to_rootsift(des: &[f32; 128]) -> [f32; 128] {
    let l1: f32 = des.iter().map(|v| v.max(0.0)).sum::<f32>() + 1e-7;
    let inv = 1.0 / l1;
    let mut out = [0.0f32; 128];
    for (dst, &v) in out.iter_mut().zip(des.iter()) {
        *dst = (v.max(0.0) * inv).sqrt();
    }
    out
}

/// Quantizes a RootSIFT descriptor to `u8` (`round(rdes * scale_factor).clamp(0, 255)`).
pub fn quantize_rootsift_u8(rdes: &[f32; 128], scale_factor: f32) -> [u8; 128] {
    let mut out = [0u8; 128];
    for (dst, &v) in out.iter_mut().zip(rdes.iter()) {
        *dst = (v * scale_factor).round().clamp(0.0, 255.0) as u8;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sift_detects_keypoints_on_textured_patch_and_rootsift_unit_l2() {
        let mut img = Array2::<u8>::zeros((96, 96));
        for y in 0..96 {
            for x in 0..96 {
                let checker = (((x / 12) + (y / 12)) % 2) as f64;
                let wave = ((x as f64 * 0.25).sin() * (y as f64 * 0.23).cos() + 1.0) * 0.5;
                img[[y, x]] = ((checker * 140.0 + wave * 100.0).clamp(0.0, 255.0)) as u8;
            }
        }
        let extractor = SiftExtractor::new(160, 0.008, 15.0);
        let (kps, des) = extractor.detect_and_compute(img.view(), None).unwrap();
        assert!(!kps.is_empty(), "expected SIFT keypoints on checkerboard");
        assert_eq!(kps.len(), des.len());
        let rdes = to_rootsift(&des[0]);
        let l2: f32 = rdes.iter().map(|v| v * v).sum();
        assert!((l2 - 1.0).abs() < 1e-3, "RootSIFT L2 norm^2={l2}");
        let q = quantize_rootsift_u8(&rdes, 400.0);
        assert!(q.iter().any(|&v| v > 0));
    }
}
