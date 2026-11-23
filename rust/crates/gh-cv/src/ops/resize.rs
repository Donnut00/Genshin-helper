//! Image resizing (`Nearest`, `Linear`, `Area` with OpenCV upscale block-replication, `Cubic`)
//! and `copy_make_border` for `u8` (2D / 3D HWC) and `f32` (2D).

use crate::ops::{BorderMode, CvError, CvResult, Interpolation, map_border_coord};
use ndarray::{Array2, Array3, ArrayView2, ArrayView3};

/// Pads a 2D `u8` array using the specified `BorderMode`.
pub fn copy_make_border_u8(
    src: ArrayView2<'_, u8>,
    top: usize,
    bottom: usize,
    left: usize,
    right: usize,
    mode: BorderMode,
) -> CvResult<Array2<u8>> {
    let (h, w) = src.dim();
    if h == 0 || w == 0 {
        return Err(CvError::EmptyInput("copy_make_border_u8 empty input".into()));
    }
    let out_h = h + top + bottom;
    let out_w = w + left + right;
    let const_v = match mode {
        BorderMode::Constant(v) => v.round().clamp(0.0, 255.0) as u8,
        _ => 0,
    };
    let mut out = Array2::<u8>::from_elem((out_h, out_w), const_v);
    let y_map: Vec<Option<usize>> = (0..out_h)
        .map(|y| map_border_coord(y as isize - top as isize, h, mode))
        .collect();
    if left == 0 && right == 0
        && let (Some(src_s), Some(out_s)) = (src.as_slice(), out.as_slice_mut())
    {
        for (dy, &sy_opt) in y_map.iter().enumerate() {
            if let Some(sy) = sy_opt {
                out_s[dy * out_w..(dy + 1) * out_w].copy_from_slice(&src_s[sy * w..(sy + 1) * w]);
            }
        }
        return Ok(out);
    }
    let x_map: Vec<Option<usize>> = (0..out_w)
        .map(|x| map_border_coord(x as isize - left as isize, w, mode))
        .collect();
    for (dy, &sy_opt) in y_map.iter().enumerate() {
        if let Some(sy) = sy_opt {
            for (dx, &sx_opt) in x_map.iter().enumerate() {
                if let Some(sx) = sx_opt {
                    out[[dy, dx]] = src[[sy, sx]];
                }
            }
        }
    }
    Ok(out)
}

/// Pads a 3D `(H, W, C)` `u8` array using the specified `BorderMode`.
pub fn copy_make_border_u8_hwc(
    src: ArrayView3<'_, u8>,
    top: usize,
    bottom: usize,
    left: usize,
    right: usize,
    mode: BorderMode,
) -> CvResult<Array3<u8>> {
    let (h, w, c) = src.dim();
    if h == 0 || w == 0 || c == 0 {
        return Err(CvError::EmptyInput("copy_make_border_u8_hwc empty input".into()));
    }
    let out_h = h + top + bottom;
    let out_w = w + left + right;
    let const_v = match mode {
        BorderMode::Constant(v) => v.round().clamp(0.0, 255.0) as u8,
        _ => 0,
    };
    let mut out = Array3::<u8>::from_elem((out_h, out_w, c), const_v);
    let y_map: Vec<Option<usize>> = (0..out_h)
        .map(|y| map_border_coord(y as isize - top as isize, h, mode))
        .collect();
    let x_map: Vec<Option<usize>> = (0..out_w)
        .map(|x| map_border_coord(x as isize - left as isize, w, mode))
        .collect();
    for (dy, &sy_opt) in y_map.iter().enumerate() {
        if let Some(sy) = sy_opt {
            for (dx, &sx_opt) in x_map.iter().enumerate() {
                if let Some(sx) = sx_opt {
                    for ch in 0..c {
                        out[[dy, dx, ch]] = src[[sy, sx, ch]];
                    }
                }
            }
        }
    }
    Ok(out)
}

/// Pads a 2D `f32` array using the specified `BorderMode`.
pub fn copy_make_border_f32(
    src: ArrayView2<'_, f32>,
    top: usize,
    bottom: usize,
    left: usize,
    right: usize,
    mode: BorderMode,
) -> CvResult<Array2<f32>> {
    let (h, w) = src.dim();
    if h == 0 || w == 0 {
        return Err(CvError::EmptyInput("copy_make_border_f32 empty input".into()));
    }
    let out_h = h + top + bottom;
    let out_w = w + left + right;
    let const_v = match mode {
        BorderMode::Constant(v) => v,
        _ => 0.0,
    };
    let mut out = Array2::<f32>::from_elem((out_h, out_w), const_v);
    let y_map: Vec<Option<usize>> = (0..out_h)
        .map(|y| map_border_coord(y as isize - top as isize, h, mode))
        .collect();
    if left == 0 && right == 0
        && let (Some(src_s), Some(out_s)) = (src.as_slice(), out.as_slice_mut())
    {
        for (dy, &sy_opt) in y_map.iter().enumerate() {
            if let Some(sy) = sy_opt {
                out_s[dy * out_w..(dy + 1) * out_w].copy_from_slice(&src_s[sy * w..(sy + 1) * w]);
            }
        }
        return Ok(out);
    }
    let x_map: Vec<Option<usize>> = (0..out_w)
        .map(|x| map_border_coord(x as isize - left as isize, w, mode))
        .collect();
    for (dy, &sy_opt) in y_map.iter().enumerate() {
        if let Some(sy) = sy_opt {
            for (dx, &sx_opt) in x_map.iter().enumerate() {
                if let Some(sx) = sx_opt {
                    out[[dy, dx]] = src[[sy, sx]];
                }
            }
        }
    }
    Ok(out)
}

#[inline]
fn cubic_weights(t: f64) -> [f64; 4] {
    const A: f64 = -0.75;
    let w0 = ((A * (t + 1.0) - 5.0 * A) * (t + 1.0) + 8.0 * A) * (t + 1.0) - 4.0 * A;
    let w1 = ((A + 2.0) * t - (A + 3.0)) * t * t + 1.0;
    let s = 1.0 - t;
    let w2 = ((A + 2.0) * s - (A + 3.0)) * s * s + 1.0;
    let w3 = 1.0 - w0 - w1 - w2;
    [w0, w1, w2, w3]
}

struct AxisTaps {
    // Packed variable-length spans per destination coordinate for Area downscale or fixed 2/4 taps
    spans: Vec<Vec<(usize, f64)>>,
}

fn build_axis_taps(src_len: usize, dst_len: usize, interp: Interpolation) -> AxisTaps {
    let mut spans = Vec::with_capacity(dst_len);
    match interp {
        Interpolation::Nearest => {
            let scale = src_len as f64 / dst_len as f64;
            for dx in 0..dst_len {
                let sx = ((dx as f64) * scale).floor() as usize;
                spans.push(vec![(sx.min(src_len - 1), 1.0)]);
            }
        }
        Interpolation::Linear => {
            let scale = src_len as f64 / dst_len as f64;
            for dx in 0..dst_len {
                let fx = (dx as f64 + 0.5) * scale - 0.5;
                let ix = fx.floor() as isize;
                let alpha = fx - ix as f64;
                let x0 = ix.clamp(0, src_len as isize - 1) as usize;
                let x1 = (ix + 1).clamp(0, src_len as isize - 1) as usize;
                if x0 == x1 {
                    spans.push(vec![(x0, 1.0)]);
                } else {
                    spans.push(vec![(x0, 1.0 - alpha), (x1, alpha)]);
                }
            }
        }
        Interpolation::Area => {
            if dst_len >= src_len {
                // OpenCV INTER_AREA upscale: block replication on integer multiples,
                // weighted boundary interpolation otherwise.
                let inv_scale = src_len as f64 / dst_len as f64;
                let scale = dst_len as f64 / src_len as f64;
                for dx in 0..dst_len {
                    let sx = ((dx as f64) * inv_scale).floor() as usize;
                    let sx_c = sx.min(src_len - 1);
                    let alpha = ((dx + 1) as f64 - (sx + 1) as f64 * scale).clamp(0.0, 1.0);
                    if alpha <= 0.0 || sx_c + 1 >= src_len {
                        spans.push(vec![(sx_c, 1.0)]);
                    } else {
                        spans.push(vec![(sx_c, 1.0 - alpha), (sx_c + 1, alpha)]);
                    }
                }
            } else {
                // Exact fractional box area overlap
                let scale = src_len as f64 / dst_len as f64;
                for dx in 0..dst_len {
                    let fx0 = dx as f64 * scale;
                    let fx1 = (dx + 1) as f64 * scale;
                    let sx0 = fx0.floor() as usize;
                    let sx1 = (fx1.ceil() as usize).min(src_len);
                    let mut row = Vec::with_capacity(sx1.saturating_sub(sx0));
                    let mut w_sum = 0.0f64;
                    for sx in sx0..sx1 {
                        let overlap = (fx1.min((sx + 1) as f64) - fx0.max(sx as f64)).max(0.0);
                        if overlap > 0.0 {
                            row.push((sx, overlap));
                            w_sum += overlap;
                        }
                    }
                    if w_sum > 0.0 {
                        let inv = 1.0 / w_sum;
                        for (_, w) in &mut row {
                            *w *= inv;
                        }
                    } else {
                        row.push((sx0.min(src_len - 1), 1.0));
                    }
                    spans.push(row);
                }
            }
        }
        Interpolation::Cubic => {
            let scale = src_len as f64 / dst_len as f64;
            let last = src_len as isize - 1;
            for dx in 0..dst_len {
                let fx = (dx as f64 + 0.5) * scale - 0.5;
                let ix = fx.floor() as isize;
                let t = fx - ix as f64;
                let w = cubic_weights(t);
                spans.push(vec![
                    ((ix - 1).clamp(0, last) as usize, w[0]),
                    (ix.clamp(0, last) as usize, w[1]),
                    ((ix + 1).clamp(0, last) as usize, w[2]),
                    ((ix + 2).clamp(0, last) as usize, w[3]),
                ]);
            }
        }
    }
    AxisTaps { spans }
}

/// Resizes a 2D `u8` grayscale image to `(dst_w, dst_h)`.
pub fn resize_u8(
    src: ArrayView2<'_, u8>,
    dst_w: usize,
    dst_h: usize,
    interp: Interpolation,
) -> CvResult<Array2<u8>> {
    let (src_h, src_w) = src.dim();
    if src_h == 0 || src_w == 0 || dst_w == 0 || dst_h == 0 {
        return Err(CvError::EmptyInput("resize_u8 zero dimension".into()));
    }
    if src_w == dst_w && src_h == dst_h {
        return Ok(src.to_owned());
    }
    let xtaps = build_axis_taps(src_w, dst_w, interp);
    let ytaps = build_axis_taps(src_h, dst_h, interp);

    if matches!(interp, Interpolation::Nearest) {
        let mut out = Array2::<u8>::zeros((dst_h, dst_w));
        for (dy, yspan) in ytaps.spans.iter().enumerate() {
            let sy = yspan[0].0;
            for (dx, xspan) in xtaps.spans.iter().enumerate() {
                out[[dy, dx]] = src[[sy, xspan[0].0]];
            }
        }
        return Ok(out);
    }

    let mut horiz = vec![0.0f64; src_h * dst_w];
    for sy in 0..src_h {
        let hrow = &mut horiz[sy * dst_w..(sy + 1) * dst_w];
        for (dx, xspan) in xtaps.spans.iter().enumerate() {
            let mut acc = 0.0f64;
            for &(sx, w) in xspan {
                acc += src[[sy, sx]] as f64 * w;
            }
            hrow[dx] = acc;
        }
    }

    let mut out = Array2::<u8>::zeros((dst_h, dst_w));
    for (dy, yspan) in ytaps.spans.iter().enumerate() {
        for dx in 0..dst_w {
            let mut acc = 0.0f64;
            for &(sy, w) in yspan {
                acc += horiz[sy * dst_w + dx] * w;
            }
            out[[dy, dx]] = acc.round().clamp(0.0, 255.0) as u8;
        }
    }
    Ok(out)
}

/// Resizes a 3D `(H, W, C)` `u8` image to `(dst_w, dst_h)`.
pub fn resize_u8_hwc(
    src: ArrayView3<'_, u8>,
    dst_w: usize,
    dst_h: usize,
    interp: Interpolation,
) -> CvResult<Array3<u8>> {
    let (src_h, src_w, c) = src.dim();
    if src_h == 0 || src_w == 0 || c == 0 || dst_w == 0 || dst_h == 0 {
        return Err(CvError::EmptyInput("resize_u8_hwc zero dimension".into()));
    }
    if src_w == dst_w && src_h == dst_h {
        return Ok(src.to_owned());
    }
    let xtaps = build_axis_taps(src_w, dst_w, interp);
    let ytaps = build_axis_taps(src_h, dst_h, interp);

    if matches!(interp, Interpolation::Nearest) {
        let mut out = Array3::<u8>::zeros((dst_h, dst_w, c));
        for (dy, yspan) in ytaps.spans.iter().enumerate() {
            let sy = yspan[0].0;
            for (dx, xspan) in xtaps.spans.iter().enumerate() {
                let sx = xspan[0].0;
                for ch in 0..c {
                    out[[dy, dx, ch]] = src[[sy, sx, ch]];
                }
            }
        }
        return Ok(out);
    }

    let mut horiz = vec![0.0f64; src_h * dst_w * c];
    for sy in 0..src_h {
        for (dx, xspan) in xtaps.spans.iter().enumerate() {
            let base = (sy * dst_w + dx) * c;
            for &(sx, w) in xspan {
                for ch in 0..c {
                    horiz[base + ch] += src[[sy, sx, ch]] as f64 * w;
                }
            }
        }
    }

    let mut out = Array3::<u8>::zeros((dst_h, dst_w, c));
    for (dy, yspan) in ytaps.spans.iter().enumerate() {
        for dx in 0..dst_w {
            let mut acc = [0.0f64; 4];
            if c <= 4 {
                for &(sy, w) in yspan {
                    let base = (sy * dst_w + dx) * c;
                    for ch in 0..c {
                        acc[ch] += horiz[base + ch] * w;
                    }
                }
                for (ch, &v) in acc.iter().take(c).enumerate() {
                    out[[dy, dx, ch]] = v.round().clamp(0.0, 255.0) as u8;
                }
            } else {
                for ch in 0..c {
                    let mut v = 0.0f64;
                    for &(sy, w) in yspan {
                        v += horiz[(sy * dst_w + dx) * c + ch] * w;
                    }
                    out[[dy, dx, ch]] = v.round().clamp(0.0, 255.0) as u8;
                }
            }
        }
    }
    Ok(out)
}

/// Resizes a 2D `f32` image to `(dst_w, dst_h)`.
pub fn resize_f32(
    src: ArrayView2<'_, f32>,
    dst_w: usize,
    dst_h: usize,
    interp: Interpolation,
) -> CvResult<Array2<f32>> {
    let (src_h, src_w) = src.dim();
    if src_h == 0 || src_w == 0 || dst_w == 0 || dst_h == 0 {
        return Err(CvError::EmptyInput("resize_f32 zero dimension".into()));
    }
    if src_w == dst_w && src_h == dst_h {
        return Ok(src.to_owned());
    }
    let xtaps = build_axis_taps(src_w, dst_w, interp);
    let ytaps = build_axis_taps(src_h, dst_h, interp);

    let mut horiz = vec![0.0f64; src_h * dst_w];
    for sy in 0..src_h {
        let hrow = &mut horiz[sy * dst_w..(sy + 1) * dst_w];
        for (dx, xspan) in xtaps.spans.iter().enumerate() {
            let mut acc = 0.0f64;
            for &(sx, w) in xspan {
                acc += src[[sy, sx]] as f64 * w;
            }
            hrow[dx] = acc;
        }
    }

    let mut out = Array2::<f32>::zeros((dst_h, dst_w));
    for (dy, yspan) in ytaps.spans.iter().enumerate() {
        for dx in 0..dst_w {
            let mut acc = 0.0f64;
            for &(sy, w) in yspan {
                acc += horiz[sy * dst_w + dx] * w;
            }
            out[[dy, dx]] = acc as f32;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ndarray::array;

    #[test]
    fn inter_area_upscale_matches_opencv_block_and_fractional() {
        let a = array![[10u8, 20, 30], [40, 50, 60]];
        let up2 = resize_u8(a.view(), 6, 4, Interpolation::Area).unwrap();
        assert_eq!(
            up2,
            array![
                [10, 10, 20, 20, 30, 30],
                [10, 10, 20, 20, 30, 30],
                [40, 40, 50, 50, 60, 60],
                [40, 40, 50, 50, 60, 60]
            ]
        );
        let up_frac = resize_u8(a.view(), 7, 5, Interpolation::Area).unwrap();
        assert_eq!(up_frac.row(0).to_vec(), vec![10, 10, 17, 20, 23, 30, 30]);
        assert_eq!(up_frac.row(2).to_vec(), vec![25, 25, 32, 35, 38, 45, 45]);
    }

    #[test]
    fn inter_area_downscale_averages_blocks() {
        let a = array![
            [10u8, 20, 30, 40],
            [10, 20, 30, 40],
            [50, 60, 70, 80],
            [50, 60, 70, 80]
        ];
        let down = resize_u8(a.view(), 2, 2, Interpolation::Area).unwrap();
        assert_eq!(down, array![[15, 35], [55, 75]]);
    }

    #[test]
    fn copy_make_border_wrap_y() {
        let a = array![[1u8, 2], [3, 4], [5, 6]];
        let padded = copy_make_border_u8(a.view(), 2, 2, 0, 0, BorderMode::Wrap).unwrap();
        assert_eq!(padded.row(0).to_vec(), vec![3, 4]);
        assert_eq!(padded.row(1).to_vec(), vec![5, 6]);
        assert_eq!(padded.row(5).to_vec(), vec![1, 2]);
        assert_eq!(padded.row(6).to_vec(), vec![3, 4]);
    }
}
