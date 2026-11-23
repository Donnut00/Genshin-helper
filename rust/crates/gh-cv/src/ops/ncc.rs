//! Normalized cross-correlation (`match_template`) for unmasked 1-ch/3-ch images
//! and Padmanabhan (2012) 6-FFT masked `TM_CCOEFF_NORMED` + fast spatial branch.

use crate::ops::{CvError, CvResult, TemplateMatchMode};
use ndarray::{Array2, ArrayView2, ArrayView3};
use rustfft::{Fft, FftPlanner, num_complex::Complex};
use std::sync::Arc;

pub type MinMaxLocResult = (f32, f32, (usize, usize), (usize, usize));

/// Returns `(min_val, max_val, (min_x, min_y), (max_x, max_y))` matching `cv2.minMaxLoc`.
pub fn min_max_loc_f32(map: ArrayView2<'_, f32>) -> CvResult<MinMaxLocResult> {
    let (h, w) = map.dim();
    if h == 0 || w == 0 {
        return Err(CvError::EmptyInput("min_max_loc_f32 empty".into()));
    }
    let mut min_val = f32::INFINITY;
    let mut max_val = f32::NEG_INFINITY;
    let mut min_loc = (0usize, 0usize);
    let mut max_loc = (0usize, 0usize);
    for y in 0..h {
        for x in 0..w {
            let v = map[[y, x]];
            if v.is_nan() {
                continue;
            }
            if v < min_val {
                min_val = v;
                min_loc = (x, y);
            }
            if v > max_val {
                max_val = v;
                max_loc = (x, y);
            }
        }
    }
    if !min_val.is_finite() {
        min_val = 0.0;
    }
    if !max_val.is_finite() {
        max_val = 0.0;
    }
    Ok((min_val, max_val, min_loc, max_loc))
}

#[inline]
pub(crate) fn next_smooth_fft_len(n: usize) -> usize {
    if n <= 1 {
        return 1;
    }
    let mut cur = n;
    loop {
        let mut x = cur;
        for p in [2usize, 3, 5] {
            while x.is_multiple_of(p) {
                x /= p;
            }
        }
        if x == 1 {
            return cur;
        }
        cur += 1;
    }
}

pub(crate) fn fft2d_c64(
    buf: &mut [Complex<f64>],
    rows: usize,
    cols: usize,
    row_fft: &Arc<dyn Fft<f64>>,
    col_fft: &Arc<dyn Fft<f64>>,
) {
    let mut scratch = vec![Complex::new(0.0, 0.0); row_fft.get_inplace_scratch_len().max(col_fft.get_inplace_scratch_len())];
    for r in 0..rows {
        let row = &mut buf[r * cols..(r + 1) * cols];
        row_fft.process_with_scratch(row, &mut scratch);
    }
    let mut col_buf = vec![Complex::new(0.0, 0.0); rows];
    for c in 0..cols {
        for r in 0..rows {
            col_buf[r] = buf[r * cols + c];
        }
        col_fft.process_with_scratch(&mut col_buf, &mut scratch);
        for r in 0..rows {
            buf[r * cols + c] = col_buf[r];
        }
    }
}

/// Computes `I ★ K` (valid cross-correlation of size `(ih - kh + 1, iw - kw + 1)`) via 2D `f64` FFT.
fn xcorr_valid_fft_f64(
    img: &[f64],
    ih: usize,
    iw: usize,
    kern: &[f64],
    kh: usize,
    kw: usize,
) -> Vec<f64> {
    let fh = next_smooth_fft_len(ih);
    let fw = next_smooth_fft_len(iw);
    let mut planner = FftPlanner::<f64>::new();
    let row_fwd = planner.plan_fft_forward(fw);
    let col_fwd = planner.plan_fft_forward(fh);
    let row_inv = planner.plan_fft_inverse(fw);
    let col_inv = planner.plan_fft_inverse(fh);

    let mut a = vec![Complex::new(0.0, 0.0); fh * fw];
    let mut b = vec![Complex::new(0.0, 0.0); fh * fw];
    for y in 0..ih {
        for x in 0..iw {
            a[y * fw + x].re = img[y * iw + x];
        }
    }
    for y in 0..kh {
        for x in 0..kw {
            b[y * fw + x].re = kern[y * kw + x];
        }
    }

    fft2d_c64(&mut a, fh, fw, &row_fwd, &col_fwd);
    fft2d_c64(&mut b, fh, fw, &row_fwd, &col_fwd);
    for (va, vb) in a.iter_mut().zip(b.iter()) {
        *va *= vb.conj();
    }
    fft2d_c64(&mut a, fh, fw, &row_inv, &col_inv);

    let norm = 1.0 / ((fh * fw) as f64);
    let oh = ih - kh + 1;
    let ow = iw - kw + 1;
    let mut out = Vec::with_capacity(oh * ow);
    for y in 0..oh {
        for x in 0..ow {
            out.push(a[y * fw + x].re * norm);
        }
    }
    out
}

/// Computes `(img1 ★ kern1, img1 ★ kern2, img2 ★ kern2)` simultaneously via complex packing.
fn xcorr_valid_pair_img_fft_f64(
    img1: &[f64],
    img2: &[f64],
    img_shape: (usize, usize),
    kern1: &[f64],
    kern2: &[f64],
    kern_shape: (usize, usize),
) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let (ih, iw) = img_shape;
    let (kh, kw) = kern_shape;
    // Returns (img1 ★ kern1, img1 ★ kern2, img2 ★ kern2) using 4 2D FFTs (fwd: img1+i*img2, kern1+i*kern2; inv: 2).
    // Actually for Padmanabhan (2012) we need:
    //   C_IT = I ★ T_zm
    //   S_I  = I ★ M
    //   S_I2 = I^2 ★ M
    // Using real signals:
    //   F_I   = FFT(I)  (1 fwd)
    //   F_I2  = FFT(I^2) (1 fwd)
    //   F_M   = FFT(M)  (1 fwd)
    //   F_T   = FFT(T_zm) (1 fwd)
    //   Pack C_IT + i * S_I  into ONE inverse FFT: IFFT(F_I * conj(F_T) + i * F_I * conj(F_M))?
    //   NO! Real signals have Hermitian symmetry, so Re(IFFT(A * conj(B) + i * C * conj(D))) != A ★ B when A,B,C,D are real!
    //   Wait: two REAL correlations with the SAME kernel M:
    //   If x = I + i * I^2, and m = M (real!), then:
    //   IFFT(FFT(I + i*I^2) * conj(FFT(M))) = (I ★ M) + i * (I^2 ★ M) !
    //   Because M is real and linear, Re is EXACTLY (I ★ M) and Im is EXACTLY (I^2 ★ M)!
    //   And then C_IT = IFFT(FFT(I) * conj(FFT(T_zm))) uses 1 fwd (T_zm) + 1 inv, OR since we already
    //   have FFT(I + i*I^2), we can extract FFT(I) from Hermitian symmetry in O(N) with ZERO extra FFT!
    //   Total 2D FFTs = 4 (fwd(I+i*I^2), fwd(M), fwd(T_zm), inv(S_I+i*S_I2), inv(C_IT)) -> 5 FFTs (or 6 standard)!
    // Let's use the rock-solid Hermitian extraction so it is 5 2D FFTs total and 100% exact!
    let fh = next_smooth_fft_len(ih);
    let fw = next_smooth_fft_len(iw);
    let mut planner = FftPlanner::<f64>::new();
    let row_fwd = planner.plan_fft_forward(fw);
    let col_fwd = planner.plan_fft_forward(fh);
    let row_inv = planner.plan_fft_inverse(fw);
    let col_inv = planner.plan_fft_inverse(fh);

    let mut f_i_pair = vec![Complex::new(0.0, 0.0); fh * fw];
    let mut f_m = vec![Complex::new(0.0, 0.0); fh * fw];
    let mut f_t = vec![Complex::new(0.0, 0.0); fh * fw];

    for y in 0..ih {
        for x in 0..iw {
            f_i_pair[y * fw + x] = Complex::new(img1[y * iw + x], img2[y * iw + x]);
        }
    }
    for y in 0..kh {
        for x in 0..kw {
            f_t[y * fw + x].re = kern1[y * kw + x];
            f_m[y * fw + x].re = kern2[y * kw + x];
        }
    }

    fft2d_c64(&mut f_i_pair, fh, fw, &row_fwd, &col_fwd);
    fft2d_c64(&mut f_m, fh, fw, &row_fwd, &col_fwd);
    fft2d_c64(&mut f_t, fh, fw, &row_fwd, &col_fwd);

    // Extract F(img1) from F(img1 + i*img2) via 2D conjugate symmetry:
    // F_re[r, c] = 0.5 * (Z[r, c] + conj(Z[(fh - r) % fh, (fw - c) % fw]))
    let mut prod_t = vec![Complex::new(0.0, 0.0); fh * fw];
    for r in 0..fh {
        let r_neg = if r == 0 { 0 } else { fh - r };
        for c in 0..fw {
            let c_neg = if c == 0 { 0 } else { fw - c };
            let z = f_i_pair[r * fw + c];
            let z_neg = f_i_pair[r_neg * fw + c_neg].conj();
            let f_img1 = (z + z_neg) * 0.5;
            prod_t[r * fw + c] = f_img1 * f_t[r * fw + c].conj();
        }
    }

    // S_I + i * S_I2 = IFFT(f_i_pair * conj(f_m))
    for (vi, vm) in f_i_pair.iter_mut().zip(f_m.iter()) {
        *vi *= vm.conj();
    }

    fft2d_c64(&mut prod_t, fh, fw, &row_inv, &col_inv);
    fft2d_c64(&mut f_i_pair, fh, fw, &row_inv, &col_inv);

    let norm = 1.0 / ((fh * fw) as f64);
    let oh = ih - kh + 1;
    let ow = iw - kw + 1;
    let mut c_it = Vec::with_capacity(oh * ow);
    let mut s_i = Vec::with_capacity(oh * ow);
    let mut s_i2 = Vec::with_capacity(oh * ow);
    for y in 0..oh {
        for x in 0..ow {
            let p_m = f_i_pair[y * fw + x];
            c_it.push(prod_t[y * fw + x].re * norm);
            s_i.push(p_m.re * norm);
            s_i2.push(p_m.im * norm);
        }
    }
    (c_it, s_i, s_i2)
}

/// Unmasked 1-channel `match_template` on `f32` images (`CcoeffNormed`, `CcorrNormed`, `SqdiffNormed`).
pub fn match_template_f32(
    img: ArrayView2<'_, f32>,
    tpl: ArrayView2<'_, f32>,
    mode: TemplateMatchMode,
) -> CvResult<Array2<f32>> {
    let (ih, iw) = img.dim();
    let (kh, kw) = tpl.dim();
    if ih == 0 || iw == 0 || kh == 0 || kw == 0 {
        return Err(CvError::EmptyInput("match_template_f32 empty".into()));
    }
    if kh > ih || kw > iw {
        return Err(CvError::ShapeMismatch("template larger than image".into()));
    }
    let oh = ih - kh + 1;
    let ow = iw - kw + 1;
    let out_cells = oh * ow;
    let n_tpl = (kh * kw) as f64;

    // Mean-shift image for numerical stability in CcoeffNormed (invariant to additive shift!)
    let mut sum_i_all = 0.0f64;
    for &v in img.iter() {
        sum_i_all += v as f64;
    }
    let mean_shift = if matches!(mode, TemplateMatchMode::CcoeffNormed) {
        sum_i_all / ((ih * iw) as f64)
    } else {
        0.0
    };

    let mut t_mean = 0.0f64;
    if matches!(mode, TemplateMatchMode::CcoeffNormed) {
        for &v in tpl.iter() {
            t_mean += v as f64;
        }
        t_mean /= n_tpl;
    }
    let mut t_vec = Vec::with_capacity(kh * kw);
    let mut t_sq_sum = 0.0f64;
    for y in 0..kh {
        for x in 0..kw {
            let tv = tpl[[y, x]] as f64 - t_mean;
            t_sq_sum += tv * tv;
            t_vec.push(tv);
        }
    }

    if matches!(mode, TemplateMatchMode::CcoeffNormed | TemplateMatchMode::CcorrNormed)
        && t_sq_sum <= 1e-12
    {
        return Ok(Array2::<f32>::zeros((oh, ow)));
    }

    // Fast spatial branch for small templates / output windows
    if out_cells <= 256 || (out_cells * kh * kw) <= 250_000 {
        let mut out = Array2::<f32>::zeros((oh, ow));
        for oy in 0..oh {
            for ox in 0..ow {
                let mut sum_i = 0.0f64;
                let mut sum_i2 = 0.0f64;
                let mut dot = 0.0f64;
                let mut sqdiff = 0.0f64;
                for ky in 0..kh {
                    let trow = &t_vec[ky * kw..(ky + 1) * kw];
                    for kx in 0..kw {
                        let iv = img[[oy + ky, ox + kx]] as f64 - mean_shift;
                        let tv = trow[kx];
                        sum_i += iv;
                        sum_i2 += iv * iv;
                        dot += iv * tv;
                        if matches!(mode, TemplateMatchMode::SqdiffNormed) {
                            let d = iv - tv;
                            sqdiff += d * d;
                        }
                    }
                }
                let val = match mode {
                    TemplateMatchMode::CcoeffNormed => {
                        let var_i = (sum_i2 - (sum_i * sum_i) / n_tpl).max(0.0);
                        let denom = (var_i * t_sq_sum).sqrt();
                        if denom <= 1e-12 || var_i <= 1e-12 * n_tpl {
                            0.0
                        } else {
                            (dot / denom).clamp(-1.0, 1.0)
                        }
                    }
                    TemplateMatchMode::CcorrNormed => {
                        let denom = (sum_i2 * t_sq_sum).sqrt();
                        if denom <= 1e-12 {
                            0.0
                        } else {
                            (dot / denom).clamp(-1.0, 1.0)
                        }
                    }
                    TemplateMatchMode::SqdiffNormed => {
                        let denom = (sum_i2 * t_sq_sum).sqrt();
                        if denom <= 1e-12 {
                            0.0
                        } else {
                            (sqdiff / denom).clamp(0.0, 1.0)
                        }
                    }
                };
                out[[oy, ox]] = if val.is_finite() { val as f32 } else { 0.0 };
            }
        }
        return Ok(out);
    }

    // Integral images for sum(I) and sum(I^2) + 2D FFT cross-correlation
    let stride_int = iw + 1;
    let mut int_i = vec![0.0f64; (ih + 1) * stride_int];
    let mut int_i2 = vec![0.0f64; (ih + 1) * stride_int];
    let mut img_shifted = Vec::with_capacity(ih * iw);
    for y in 0..ih {
        let mut row_s1 = 0.0f64;
        let mut row_s2 = 0.0f64;
        for x in 0..iw {
            let v = img[[y, x]] as f64 - mean_shift;
            img_shifted.push(v);
            row_s1 += v;
            row_s2 += v * v;
            let idx = (y + 1) * stride_int + (x + 1);
            int_i[idx] = int_i[y * stride_int + (x + 1)] + row_s1;
            int_i2[idx] = int_i2[y * stride_int + (x + 1)] + row_s2;
        }
    }

    let corr = xcorr_valid_fft_f64(&img_shifted, ih, iw, &t_vec, kh, kw);
    let mut out = Array2::<f32>::zeros((oh, ow));
    for oy in 0..oh {
        let y0 = oy;
        let y1 = oy + kh;
        for ox in 0..ow {
            let x0 = ox;
            let x1 = ox + kw;
            let s1 = int_i[y1 * stride_int + x1]
                - int_i[y0 * stride_int + x1]
                - int_i[y1 * stride_int + x0]
                + int_i[y0 * stride_int + x0];
            let s2 = int_i2[y1 * stride_int + x1]
                - int_i2[y0 * stride_int + x1]
                - int_i2[y1 * stride_int + x0]
                + int_i2[y0 * stride_int + x0];
            let dot = corr[oy * ow + ox];
            let val = match mode {
                TemplateMatchMode::CcoeffNormed => {
                    let var_i = (s2 - (s1 * s1) / n_tpl).max(0.0);
                    let denom = (var_i * t_sq_sum).sqrt();
                    if denom <= 1e-12 || var_i <= 1e-12 * n_tpl {
                        0.0
                    } else {
                        (dot / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::CcorrNormed => {
                    let denom = (s2.max(0.0) * t_sq_sum).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        (dot / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::SqdiffNormed => {
                    let denom = (s2.max(0.0) * t_sq_sum).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        ((s2 - 2.0 * dot + t_sq_sum).max(0.0) / denom).clamp(0.0, 1.0)
                    }
                }
            };
            out[[oy, ox]] = if val.is_finite() { val as f32 } else { 0.0 };
        }
    }
    Ok(out)
}

/// Unmasked 1-channel `match_template` on `u8` grayscale images.
pub fn match_template_u8(
    img: ArrayView2<'_, u8>,
    tpl: ArrayView2<'_, u8>,
    mode: TemplateMatchMode,
) -> CvResult<Array2<f32>> {
    let img_f = img.mapv(|v| v as f32);
    let tpl_f = tpl.mapv(|v| v as f32);
    match_template_f32(img_f.view(), tpl_f.view(), mode)
}

/// Unmasked 3-channel `(H, W, 3)` `match_template` on `u8` BGR/RGB images matching `cv2.matchTemplate`.
pub fn match_template_u8_hwc(
    img: ArrayView3<'_, u8>,
    tpl: ArrayView3<'_, u8>,
    mode: TemplateMatchMode,
) -> CvResult<Array2<f32>> {
    let (ih, iw, ic) = img.dim();
    let (kh, kw, kc) = tpl.dim();
    if ih == 0 || iw == 0 || kh == 0 || kw == 0 {
        return Err(CvError::EmptyInput("match_template_u8_hwc empty".into()));
    }
    if ic != kc || ic == 0 {
        return Err(CvError::ShapeMismatch("channel mismatch".into()));
    }
    if kh > ih || kw > iw {
        return Err(CvError::ShapeMismatch("template larger than image".into()));
    }

    let oh = ih - kh + 1;
    let ow = iw - kw + 1;
    let out_cells = oh * ow;
    let n_tpl = (kh * kw) as f64;

    let mut t_means = vec![0.0f64; ic];
    if matches!(mode, TemplateMatchMode::CcoeffNormed) {
        for y in 0..kh {
            for x in 0..kw {
                for c in 0..ic {
                    t_means[c] += tpl[[y, x, c]] as f64;
                }
            }
        }
        for m in &mut t_means {
            *m /= n_tpl;
        }
    }
    let mut t_sq_total = 0.0f64;
    let mut t_planes = vec![vec![0.0f64; kh * kw]; ic];
    for c in 0..ic {
        for y in 0..kh {
            for x in 0..kw {
                let tv = tpl[[y, x, c]] as f64 - t_means[c];
                t_planes[c][y * kw + x] = tv;
                t_sq_total += tv * tv;
            }
        }
    }

    if matches!(mode, TemplateMatchMode::CcoeffNormed | TemplateMatchMode::CcorrNormed)
        && t_sq_total <= 1e-12
    {
        return Ok(Array2::<f32>::zeros((oh, ow)));
    }

    let mut num_sum = vec![0.0f64; out_cells];
    let mut var_i_sum = vec![0.0f64; out_cells];
    let mut sq_i_sum = vec![0.0f64; out_cells];

    for c in 0..ic {
        if out_cells <= 256 || (out_cells * kh * kw) <= 250_000 {
            let t_plane = &t_planes[c];
            for oy in 0..oh {
                for ox in 0..ow {
                    let mut s1 = 0.0f64;
                    let mut s2 = 0.0f64;
                    let mut dot = 0.0f64;
                    for ky in 0..kh {
                        let trow = &t_plane[ky * kw..(ky + 1) * kw];
                        for kx in 0..kw {
                            let iv = img[[oy + ky, ox + kx, c]] as f64;
                            s1 += iv;
                            s2 += iv * iv;
                            dot += iv * trow[kx];
                        }
                    }
                    let idx = oy * ow + ox;
                    num_sum[idx] += dot;
                    sq_i_sum[idx] += s2;
                    var_i_sum[idx] += (s2 - (s1 * s1) / n_tpl).max(0.0);
                }
            }
        } else {
            let stride_int = iw + 1;
            let mut int_i = vec![0.0f64; (ih + 1) * stride_int];
            let mut int_i2 = vec![0.0f64; (ih + 1) * stride_int];
            let mut plane = Vec::with_capacity(ih * iw);
            for y in 0..ih {
                let mut rs1 = 0.0f64;
                let mut rs2 = 0.0f64;
                for x in 0..iw {
                    let v = img[[y, x, c]] as f64;
                    plane.push(v);
                    rs1 += v;
                    rs2 += v * v;
                    let idx = (y + 1) * stride_int + (x + 1);
                    int_i[idx] = int_i[y * stride_int + (x + 1)] + rs1;
                    int_i2[idx] = int_i2[y * stride_int + (x + 1)] + rs2;
                }
            }
            let corr = xcorr_valid_fft_f64(&plane, ih, iw, &t_planes[c], kh, kw);
            for oy in 0..oh {
                let y0 = oy;
                let y1 = oy + kh;
                for ox in 0..ow {
                    let x0 = ox;
                    let x1 = ox + kw;
                    let s1 = int_i[y1 * stride_int + x1]
                        - int_i[y0 * stride_int + x1]
                        - int_i[y1 * stride_int + x0]
                        + int_i[y0 * stride_int + x0];
                    let s2 = int_i2[y1 * stride_int + x1]
                        - int_i2[y0 * stride_int + x1]
                        - int_i2[y1 * stride_int + x0]
                        + int_i2[y0 * stride_int + x0];
                    let idx = oy * ow + ox;
                    num_sum[idx] += corr[idx];
                    sq_i_sum[idx] += s2.max(0.0);
                    var_i_sum[idx] += (s2 - (s1 * s1) / n_tpl).max(0.0);
                }
            }
        }
    }

    let mut out = Array2::<f32>::zeros((oh, ow));
    for oy in 0..oh {
        for ox in 0..ow {
            let idx = oy * ow + ox;
            let val = match mode {
                TemplateMatchMode::CcoeffNormed => {
                    let denom = (var_i_sum[idx] * t_sq_total).sqrt();
                    if denom <= 1e-12 || var_i_sum[idx] <= 1e-12 * n_tpl * (ic as f64) {
                        0.0
                    } else {
                        (num_sum[idx] / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::CcorrNormed => {
                    let denom = (sq_i_sum[idx] * t_sq_total).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        (num_sum[idx] / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::SqdiffNormed => {
                    let denom = (sq_i_sum[idx] * t_sq_total).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        ((sq_i_sum[idx] - 2.0 * num_sum[idx] + t_sq_total).max(0.0) / denom)
                            .clamp(0.0, 1.0)
                    }
                }
            };
            out[[oy, ox]] = if val.is_finite() { val as f32 } else { 0.0 };
        }
    }
    Ok(out)
}

/// Masked `match_template` on `f32` images (`mask > 0` active pixels).
/// Uses fast spatial sparse-mask inner loop for small outputs (`<= 256` cells) or small templates,
/// and Padmanabhan (2012) FFT masked NCC (`I, I^2, M, MT`) in `f64` for large patches.
pub fn match_template_masked_f32(
    img: ArrayView2<'_, f32>,
    tpl: ArrayView2<'_, f32>,
    mask: ArrayView2<'_, u8>,
    mode: TemplateMatchMode,
) -> CvResult<Array2<f32>> {
    let (ih, iw) = img.dim();
    let (kh, kw) = tpl.dim();
    if ih == 0 || iw == 0 || kh == 0 || kw == 0 {
        return Err(CvError::EmptyInput("match_template_masked_f32 empty".into()));
    }
    if tpl.dim() != mask.dim() {
        return Err(CvError::ShapeMismatch("tpl and mask dimensions differ".into()));
    }
    if kh > ih || kw > iw {
        return Err(CvError::ShapeMismatch("template larger than image".into()));
    }

    let oh = ih - kh + 1;
    let ow = iw - kw + 1;
    let out_cells = oh * ow;

    let mut active_pts: Vec<(usize, usize, f64)> = Vec::new();
    let mut sum_mt = 0.0f64;
    for ky in 0..kh {
        for kx in 0..kw {
            if mask[[ky, kx]] > 0 {
                let tv = tpl[[ky, kx]] as f64;
                active_pts.push((ky, kx, tv));
                sum_mt += tv;
            }
        }
    }
    let n_m = active_pts.len() as f64;
    if active_pts.is_empty() {
        return Ok(Array2::<f32>::zeros((oh, ow)));
    }
    let t_mean = if matches!(mode, TemplateMatchMode::CcoeffNormed) {
        sum_mt / n_m
    } else {
        0.0
    };

    let mut t_zm = vec![0.0f64; kh * kw];
    let mut m_bin = vec![0.0f64; kh * kw];
    let mut t_var = 0.0f64;
    for &(ky, kx, tv) in &active_pts {
        let d = tv - t_mean;
        t_zm[ky * kw + kx] = d;
        m_bin[ky * kw + kx] = 1.0;
        t_var += d * d;
    }

    if matches!(mode, TemplateMatchMode::CcoeffNormed | TemplateMatchMode::CcorrNormed)
        && t_var <= 1e-12
    {
        return Ok(Array2::<f32>::zeros((oh, ow)));
    }

    // Spatial SIMD-friendly sparse mask path for small ROIs / Paimon refine (<= 256 cells)
    if out_cells <= 256 || (out_cells * active_pts.len()) <= 180_000 {
        let active_zm: Vec<(usize, usize, f64)> = active_pts
            .iter()
            .map(|&(ky, kx, tv)| (ky, kx, tv - t_mean))
            .collect();
        let mut out = Array2::<f32>::zeros((oh, ow));
        for oy in 0..oh {
            for ox in 0..ow {
                let mut s_i = 0.0f64;
                let mut s_i2 = 0.0f64;
                let mut dot = 0.0f64;
                let mut sqdiff = 0.0f64;
                for &(ky, kx, tv) in &active_zm {
                    let iv = img[[oy + ky, ox + kx]] as f64;
                    s_i += iv;
                    s_i2 += iv * iv;
                    dot += iv * tv;
                    if matches!(mode, TemplateMatchMode::SqdiffNormed) {
                        let d = iv - tv;
                        sqdiff += d * d;
                    }
                }
                let val = match mode {
                    TemplateMatchMode::CcoeffNormed => {
                        let var_i = (s_i2 - (s_i * s_i) / n_m).max(0.0);
                        let denom = (var_i * t_var).sqrt();
                        if denom <= 1e-12 || var_i <= 1e-12 * n_m {
                            0.0
                        } else {
                            (dot / denom).clamp(-1.0, 1.0)
                        }
                    }
                    TemplateMatchMode::CcorrNormed => {
                        let denom = (s_i2.max(0.0) * t_var).sqrt();
                        if denom <= 1e-12 {
                            0.0
                        } else {
                            (dot / denom).clamp(-1.0, 1.0)
                        }
                    }
                    TemplateMatchMode::SqdiffNormed => {
                        let denom = (s_i2.max(0.0) * t_var).sqrt();
                        if denom <= 1e-12 {
                            0.0
                        } else {
                            (sqdiff / denom).max(0.0)
                        }
                    }
                };
                out[[oy, ox]] = if val.is_finite() { val as f32 } else { 0.0 };
            }
        }
        return Ok(out);
    }

    // Padmanabhan (2012) masked NCC via 2D FFT
    let mut sum_i_all = 0.0f64;
    for &v in img.iter() {
        sum_i_all += v as f64;
    }
    let shift = if matches!(mode, TemplateMatchMode::CcoeffNormed) {
        sum_i_all / ((ih * iw) as f64)
    } else {
        0.0
    };

    let mut i_arr = Vec::with_capacity(ih * iw);
    let mut i2_arr = Vec::with_capacity(ih * iw);
    for y in 0..ih {
        for x in 0..iw {
            let v = img[[y, x]] as f64 - shift;
            i_arr.push(v);
            i2_arr.push(v * v);
        }
    }

    let (c_it, s_i, s_i2) =
        xcorr_valid_pair_img_fft_f64(&i_arr, &i2_arr, (ih, iw), &t_zm, &m_bin, (kh, kw));

    let mut out = Array2::<f32>::zeros((oh, ow));
    for oy in 0..oh {
        for ox in 0..ow {
            let idx = oy * ow + ox;
            let val = match mode {
                TemplateMatchMode::CcoeffNormed => {
                    let var_i = (s_i2[idx] - (s_i[idx] * s_i[idx]) / n_m).max(0.0);
                    let denom = (var_i * t_var).sqrt();
                    if denom <= 1e-12 || var_i <= 1e-12 * n_m {
                        0.0
                    } else {
                        (c_it[idx] / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::CcorrNormed => {
                    let denom = (s_i2[idx].max(0.0) * t_var).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        (c_it[idx] / denom).clamp(-1.0, 1.0)
                    }
                }
                TemplateMatchMode::SqdiffNormed => {
                    let denom = (s_i2[idx].max(0.0) * t_var).sqrt();
                    if denom <= 1e-12 {
                        0.0
                    } else {
                        ((s_i2[idx] - 2.0 * c_it[idx] + t_var).max(0.0) / denom).max(0.0)
                    }
                }
            };
            out[[oy, ox]] = if val.is_finite() { val as f32 } else { 0.0 };
        }
    }
    Ok(out)
}

/// Masked `match_template` on `u8` grayscale images (`mask > 0` active pixels).
pub fn match_template_masked_u8(
    img: ArrayView2<'_, u8>,
    tpl: ArrayView2<'_, u8>,
    mask: ArrayView2<'_, u8>,
    mode: TemplateMatchMode,
) -> CvResult<Array2<f32>> {
    let img_f = img.mapv(|v| v as f32);
    let tpl_f = tpl.mapv(|v| v as f32);
    match_template_masked_f32(img_f.view(), tpl_f.view(), mask, mode)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unmasked_and_masked_ncc_find_exact_peak() {
        let mut img = Array2::<f32>::zeros((64, 64));
        for y in 0..64 {
            for x in 0..64 {
                img[[y, x]] = (((y * 7 + x * 13) % 97) + ((y * x) % 53)) as f32;
            }
        }
        let tpl = img.slice(ndarray::s![18..38, 22..42]).to_owned();
        let res = match_template_f32(img.view(), tpl.view(), TemplateMatchMode::CcoeffNormed).unwrap();
        let (_, max_v, _, max_loc) = min_max_loc_f32(res.view()).unwrap();
        assert!((max_v - 1.0).abs() < 1e-4, "max_v={max_v}");
        assert_eq!(max_loc, (22, 18));

        let mut mask = Array2::<u8>::zeros((20, 20));
        for y in 2..18 {
            for x in 2..18 {
                if (y as i32 - 10).pow(2) + (x as i32 - 10).pow(2) <= 49 {
                    mask[[y, x]] = 255;
                }
            }
        }
        let res_m =
            match_template_masked_f32(img.view(), tpl.view(), mask.view(), TemplateMatchMode::CcoeffNormed)
                .unwrap();
        let (_, mv_m, _, ml_m) = min_max_loc_f32(res_m.view()).unwrap();
        assert!((mv_m - 1.0).abs() < 1e-4, "mv_m={mv_m}");
        assert_eq!(ml_m, (22, 18));
    }

    #[test]
    fn zero_variance_returns_zero_never_nan() {
        let flat = Array2::<u8>::from_elem((40, 40), 128);
        let tpl = Array2::<u8>::from_elem((16, 16), 128);
        let mask = Array2::<u8>::from_elem((16, 16), 255);
        let res =
            match_template_masked_u8(flat.view(), tpl.view(), mask.view(), TemplateMatchMode::CcoeffNormed)
                .unwrap();
        assert!(res.iter().all(|&v| v == 0.0 && v.is_finite()));

        // Unmasked flat patch alongside high-contrast region (tests mean_shift cancellation guard)
        let mut img_f = Array2::<f32>::zeros((80, 80));
        for y in 0..40 {
            for x in 0..80 {
                img_f[[y, x]] = 5000.0 + ((y * 13 + x * 7) % 100) as f32;
            }
        }
        let tpl_f = img_f.slice(ndarray::s![0..20, 0..20]).to_owned();
        let res_u =
            match_template_f32(img_f.view(), tpl_f.view(), TemplateMatchMode::CcoeffNormed).unwrap();
        // Bottom rows (y >= 40) are pure 0.0 constant windows and must return exact 0.0
        for y in 40..res_u.dim().0 {
            for x in 0..res_u.dim().1 {
                assert_eq!(res_u[[y, x]], 0.0, "flat window ({y},{x}) returned {}", res_u[[y, x]]);
            }
        }
    }

    #[test]
    fn unmasked_sqdiff_normed_clamped_to_unit_interval() {
        let mut img = Array2::<f32>::from_elem((80, 80), 1.0);
        img[[10, 10]] = 2.0;
        let tpl = Array2::<f32>::from_elem((20, 20), 250.0);
        let res = match_template_f32(img.view(), tpl.view(), TemplateMatchMode::SqdiffNormed).unwrap();
        assert!(res.iter().all(|&v| (0.0..=1.0).contains(&v)), "SqdiffNormed f32 exceeded 1.0");

        // Spatial branch (small output)
        let small_img = Array2::<f32>::from_elem((10, 10), 2.0);
        let small_tpl = Array2::<f32>::from_elem((5, 5), 200.0);
        let res_sp =
            match_template_f32(small_img.view(), small_tpl.view(), TemplateMatchMode::SqdiffNormed)
                .unwrap();
        assert!(res_sp.iter().all(|&v| (0.0..=1.0).contains(&v)));

        // HWC branch
        let img3 = ndarray::Array3::<u8>::from_elem((40, 40, 3), 2);
        let tpl3 = ndarray::Array3::<u8>::from_elem((16, 16, 3), 200);
        let res_hwc =
            match_template_u8_hwc(img3.view(), tpl3.view(), TemplateMatchMode::SqdiffNormed)
                .unwrap();
        assert!(res_hwc.iter().all(|&v| (0.0..=1.0).contains(&v)));
    }
}
