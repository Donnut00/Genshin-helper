//! Morphological operations (`StructuringElement`, `erode`, `dilate`, `open`, `close`)
//! and mask rasterizers (`draw_filled_circle`, `draw_filled_ellipse_sector`, `draw_filled_convex_poly`).

use crate::ops::{CvError, CvResult, MorphOp, MorphShape, Point2i};
use ndarray::{Array2, ArrayView2};

/// Creates a structuring element matching `cv2.getStructuringElement`.
pub fn get_structuring_element(shape: MorphShape, w: usize, h: usize) -> CvResult<Array2<u8>> {
    if w == 0 || h == 0 {
        return Err(CvError::InvalidArgument("structuring element dims must be >= 1".into()));
    }
    let mut k = Array2::<u8>::zeros((h, w));
    match shape {
        MorphShape::Rect => k.fill(1),
        MorphShape::Ellipse => {
            if w == 1 && h == 1 {
                k[[0, 0]] = 1;
                return Ok(k);
            }
            let cx = w / 2;
            let cy = h / 2;
            let inv_r2 = if cy > 0 {
                1.0 / ((cy as f64) * (cy as f64))
            } else {
                0.0
            };
            for y in 0..h {
                let (x1, x2) = if y == cy || cy == 0 {
                    (0, w)
                } else {
                    let dy = y as isize - cy as isize;
                    let r2_minus_dy2 = ((cy as isize) * (cy as isize) - dy * dy) as f64;
                    let dx = (cx as f64) * (r2_minus_dy2 * inv_r2).sqrt();
                    let rx_round = dx.round_ties_even() as usize;
                    (cx.saturating_sub(rx_round), (cx + rx_round + 1).min(w))
                };
                for x in x1..x2 {
                    k[[y, x]] = 1;
                }
            }
        }
    }
    Ok(k)
}

fn kernel_offsets(kernel: ArrayView2<'_, u8>) -> CvResult<Vec<(isize, isize)>> {
    let (kh, kw) = kernel.dim();
    if kh == 0 || kw == 0 {
        return Err(CvError::EmptyInput("empty morph kernel".into()));
    }
    let ax = (kw / 2) as isize;
    let ay = (kh / 2) as isize;
    let mut offs = Vec::new();
    for ky in 0..kh {
        for kx in 0..kw {
            if kernel[[ky, kx]] != 0 {
                offs.push((kx as isize - ax, ky as isize - ay));
            }
        }
    }
    Ok(offs)
}

/// Erodes a 2D `u8` image (OOB border = 255 matching OpenCV `morphologyDefaultBorderValue`).
pub fn erode_u8(src: ArrayView2<'_, u8>, kernel: ArrayView2<'_, u8>) -> CvResult<Array2<u8>> {
    let (h, w) = src.dim();
    if h == 0 || w == 0 {
        return Err(CvError::EmptyInput("erode_u8 empty".into()));
    }
    let offs = kernel_offsets(kernel)?;
    if offs.is_empty() {
        return Ok(src.to_owned());
    }
    let src_contig;
    let s = if let Some(sl) = src.as_slice() {
        sl
    } else {
        src_contig = src.to_owned();
        src_contig.as_slice().unwrap()
    };
    let mut out = Array2::<u8>::zeros((h, w));
    let dst = out.as_slice_mut().unwrap();

    // Fast path for (2, 1) horizontal kernel (anchor (1, 0): offsets (-1, 0), (0, 0))
    if offs.as_slice() == [(-1, 0), (0, 0)] {
        for y in 0..h {
            let srow = &s[y * w..(y + 1) * w];
            let drow = &mut dst[y * w..(y + 1) * w];
            drow[0] = srow[0];
            for x in 1..w {
                drow[x] = srow[x - 1].min(srow[x]);
            }
        }
        return Ok(out);
    }

    for y in 0..h {
        let drow = &mut dst[y * w..(y + 1) * w];
        for (x, dpx) in drow.iter_mut().enumerate() {
            let mut m = 255u8;
            for &(dx, dy) in &offs {
                let sx = x as isize + dx;
                let sy = y as isize + dy;
                if sx >= 0 && sx < w as isize && sy >= 0 && sy < h as isize {
                    m = m.min(s[sy as usize * w + sx as usize]);
                }
            }
            *dpx = m;
        }
    }
    Ok(out)
}

/// Dilates a 2D `u8` image (OOB border = 0 matching OpenCV `morphologyDefaultBorderValue`).
pub fn dilate_u8(src: ArrayView2<'_, u8>, kernel: ArrayView2<'_, u8>) -> CvResult<Array2<u8>> {
    let (h, w) = src.dim();
    if h == 0 || w == 0 {
        return Err(CvError::EmptyInput("dilate_u8 empty".into()));
    }
    let offs = kernel_offsets(kernel)?;
    if offs.is_empty() {
        return Ok(src.to_owned());
    }
    let src_contig;
    let s = if let Some(sl) = src.as_slice() {
        sl
    } else {
        src_contig = src.to_owned();
        src_contig.as_slice().unwrap()
    };
    let mut out = Array2::<u8>::zeros((h, w));
    let dst = out.as_slice_mut().unwrap();

    // Fast path for (2, 1) horizontal kernel (anchor (1, 0): offsets (-1, 0), (0, 0))
    if offs.as_slice() == [(-1, 0), (0, 0)] {
        for y in 0..h {
            let srow = &s[y * w..(y + 1) * w];
            let drow = &mut dst[y * w..(y + 1) * w];
            drow[0] = srow[0];
            for x in 1..w {
                drow[x] = srow[x - 1].max(srow[x]);
            }
        }
        return Ok(out);
    }

    for y in 0..h {
        let drow = &mut dst[y * w..(y + 1) * w];
        for (x, dpx) in drow.iter_mut().enumerate() {
            let mut m = 0u8;
            for &(dx, dy) in &offs {
                let sx = x as isize + dx;
                let sy = y as isize + dy;
                if sx >= 0 && sx < w as isize && sy >= 0 && sy < h as isize {
                    m = m.max(s[sy as usize * w + sx as usize]);
                }
            }
            *dpx = m;
        }
    }
    Ok(out)
}

/// Applies morphological operation (`Erode`, `Dilate`, `Open`, `Close`).
pub fn morphology_ex_u8(
    src: ArrayView2<'_, u8>,
    op: MorphOp,
    kernel: ArrayView2<'_, u8>,
) -> CvResult<Array2<u8>> {
    match op {
        MorphOp::Erode => erode_u8(src, kernel),
        MorphOp::Dilate => dilate_u8(src, kernel),
        MorphOp::Open => {
            let e = erode_u8(src, kernel)?;
            dilate_u8(e.view(), kernel)
        }
        MorphOp::Close => {
            let d = dilate_u8(src, kernel)?;
            erode_u8(d.view(), kernel)
        }
    }
}

/// Rasterizes a filled circle `(cx, cy, radius)` into a 2D `u8` mask.
pub fn draw_filled_circle_u8(
    buf: &mut Array2<u8>,
    cx: i32,
    cy: i32,
    radius: i32,
    val: u8,
) {
    if radius < 0 {
        return;
    }
    let (h, w) = buf.dim();
    let r2 = (radius as i64) * (radius as i64);
    let y0 = (cy - radius).max(0) as usize;
    let y1 = (cy + radius).min(h as i32 - 1).max(-1) as isize;
    let x0 = (cx - radius).max(0) as usize;
    let x1 = (cx + radius).min(w as i32 - 1).max(-1) as isize;
    if y1 < y0 as isize || x1 < x0 as isize {
        return;
    }
    for y in y0..=(y1 as usize) {
        let dy = y as i64 - cy as i64;
        for x in x0..=(x1 as usize) {
            let dx = x as i64 - cx as i64;
            if dx * dx + dy * dy <= r2 {
                buf[[y, x]] = val;
            }
        }
    }
}

/// Rasterizes a filled circle `(cx, cy, radius)` into a 2D `f32` buffer.
pub fn draw_filled_circle_f32(
    buf: &mut Array2<f32>,
    cx: i32,
    cy: i32,
    radius: i32,
    val: f32,
) {
    if radius < 0 {
        return;
    }
    let (h, w) = buf.dim();
    let r2 = (radius as i64) * (radius as i64);
    let y0 = (cy - radius).max(0) as usize;
    let y1 = (cy + radius).min(h as i32 - 1).max(-1) as isize;
    let x0 = (cx - radius).max(0) as usize;
    let x1 = (cx + radius).min(w as i32 - 1).max(-1) as isize;
    if y1 < y0 as isize || x1 < x0 as isize {
        return;
    }
    for y in y0..=(y1 as usize) {
        let dy = y as i64 - cy as i64;
        for x in x0..=(x1 as usize) {
            let dx = x as i64 - cx as i64;
            if dx * dx + dy * dy <= r2 {
                buf[[y, x]] = val;
            }
        }
    }
}

#[inline]
fn angle_in_arc(ang_deg: f64, start_deg: f64, end_deg: f64) -> bool {
    let span = end_deg - start_deg;
    if span.abs() >= 360.0 {
        return true;
    }
    let lo = start_deg.min(end_deg);
    let hi = start_deg.max(end_deg);
    // Shift ang_deg by multiples of 360 to test interval [lo, hi]
    let k = ((lo - ang_deg) / 360.0).ceil();
    let a = ang_deg + k * 360.0;
    a >= lo - 1e-6 && a <= hi + 1e-6
}

/// Rasterizes a filled elliptical sector matching `cv2.ellipse(..., angle=0, startAngle, endAngle, thickness=-1)`
/// where `0 deg` is `+X` and positive angles increase clockwise in image coordinates (`+Y` down).
#[allow(clippy::too_many_arguments)]
pub fn draw_filled_ellipse_sector_u8(
    buf: &mut Array2<u8>,
    cx: f64,
    cy: f64,
    rx: f64,
    ry: f64,
    start_deg: f64,
    end_deg: f64,
    val: u8,
) {
    if rx <= 0.0 || ry <= 0.0 {
        return;
    }
    let (h, w) = buf.dim();
    let inv_rx2 = 1.0 / (rx * rx);
    let inv_ry2 = 1.0 / (ry * ry);
    let y0 = ((cy - ry).floor() as isize).max(0) as usize;
    let y1 = ((cy + ry).ceil() as isize).min(h as isize - 1);
    let x0 = ((cx - rx).floor() as isize).max(0) as usize;
    let x1 = ((cx + rx).ceil() as isize).min(w as isize - 1);
    if y1 < y0 as isize || x1 < x0 as isize {
        return;
    }
    for y in y0..=(y1 as usize) {
        let dy = y as f64 - cy;
        for x in x0..=(x1 as usize) {
            let dx = x as f64 - cx;
            if dx * dx * inv_rx2 + dy * dy * inv_ry2 <= 1.0 {
                if dx == 0.0 && dy == 0.0 {
                    buf[[y, x]] = val;
                } else {
                    let ang = dy.atan2(dx).to_degrees();
                    if angle_in_arc(ang, start_deg, end_deg) {
                        buf[[y, x]] = val;
                    }
                }
            }
        }
    }
}

/// Rasterizes a filled elliptical sector into a 2D `f32` buffer.
#[allow(clippy::too_many_arguments)]
pub fn draw_filled_ellipse_sector_f32(
    buf: &mut Array2<f32>,
    cx: f64,
    cy: f64,
    rx: f64,
    ry: f64,
    start_deg: f64,
    end_deg: f64,
    val: f32,
) {
    if rx <= 0.0 || ry <= 0.0 {
        return;
    }
    let (h, w) = buf.dim();
    let inv_rx2 = 1.0 / (rx * rx);
    let inv_ry2 = 1.0 / (ry * ry);
    let y0 = ((cy - ry).floor() as isize).max(0) as usize;
    let y1 = ((cy + ry).ceil() as isize).min(h as isize - 1);
    let x0 = ((cx - rx).floor() as isize).max(0) as usize;
    let x1 = ((cx + rx).ceil() as isize).min(w as isize - 1);
    if y1 < y0 as isize || x1 < x0 as isize {
        return;
    }
    for y in y0..=(y1 as usize) {
        let dy = y as f64 - cy;
        for x in x0..=(x1 as usize) {
            let dx = x as f64 - cx;
            if dx * dx * inv_rx2 + dy * dy * inv_ry2 <= 1.0 {
                if dx == 0.0 && dy == 0.0 {
                    buf[[y, x]] = val;
                } else {
                    let ang = dy.atan2(dx).to_degrees();
                    if angle_in_arc(ang, start_deg, end_deg) {
                        buf[[y, x]] = val;
                    }
                }
            }
        }
    }
}

/// Rasterizes a filled polygon using scanline even-odd fill matching `cv2.fillConvexPoly`.
pub fn draw_filled_convex_poly_u8(buf: &mut Array2<u8>, pts: &[Point2i], val: u8) {
    if pts.len() < 3 {
        return;
    }
    let (h, w) = buf.dim();
    let min_y = pts.iter().map(|p| p.y).min().unwrap_or(0).max(0) as usize;
    let max_y = pts
        .iter()
        .map(|p| p.y)
        .max()
        .unwrap_or(0)
        .min(h as i32 - 1);
    if max_y < min_y as i32 {
        return;
    }
    let n = pts.len();
    let mut xs = Vec::with_capacity(n);
    for y in min_y..=(max_y as usize) {
        xs.clear();
        let yf = y as f64;
        for i in 0..n {
            let p0 = pts[i];
            let p1 = pts[(i + 1) % n];
            let y0 = p0.y as f64;
            let y1 = p1.y as f64;
            if (y0 <= yf && y1 > yf) || (y1 <= yf && y0 > yf) {
                let x = p0.x as f64 + (yf - y0) * (p1.x - p0.x) as f64 / (y1 - y0);
                xs.push(x);
            }
        }
        xs.sort_by(|a, b| a.total_cmp(b));
        for pair in xs.as_chunks::<2>().0 {
            let x_start = (pair[0].ceil() as isize).max(0) as usize;
            let x_end = (pair[1].floor() as isize).min(w as isize - 1);
            if x_end >= x_start as isize {
                for x in x_start..=(x_end as usize) {
                    buf[[y, x]] = val;
                }
            }
        }
    }
    // Rasterize exact polygon edges as well (matching OpenCV fillConvexPoly boundary inclusion)
    for i in 0..n {
        let mut x0 = pts[i].x;
        let mut y0 = pts[i].y;
        let x1 = pts[(i + 1) % n].x;
        let y1 = pts[(i + 1) % n].y;
        let dx = (x1 - x0).abs();
        let sx = if x0 < x1 { 1 } else { -1 };
        let dy = -(y1 - y0).abs();
        let sy = if y0 < y1 { 1 } else { -1 };
        let mut err = dx + dy;
        loop {
            if x0 >= 0 && x0 < w as i32 && y0 >= 0 && y0 < h as i32 {
                buf[[y0 as usize, x0 as usize]] = val;
            }
            if x0 == x1 && y0 == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 >= dy {
                err += dy;
                x0 += sx;
            }
            if e2 <= dx {
                err += dx;
                y0 += sy;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ellipse_3x3_and_morph_open_work() {
        let se = get_structuring_element(MorphShape::Ellipse, 3, 3).unwrap();
        assert_eq!(se[[0, 0]], 0);
        assert_eq!(se[[0, 1]], 1);
        assert_eq!(se[[1, 1]], 1);

        let se44 = get_structuring_element(MorphShape::Ellipse, 4, 4).unwrap();
        assert_eq!(
            se44,
            ndarray::array![
                [0, 0, 1, 0],
                [1, 1, 1, 1],
                [1, 1, 1, 1],
                [1, 1, 1, 1],
            ]
        );
        let se55 = get_structuring_element(MorphShape::Ellipse, 5, 5).unwrap();
        assert_eq!(
            se55,
            ndarray::array![
                [0, 0, 1, 0, 0],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0],
            ]
        );
        let se75 = get_structuring_element(MorphShape::Ellipse, 7, 5).unwrap();
        assert_eq!(
            se75,
            ndarray::array![
                [0, 0, 0, 1, 0, 0, 0],
                [1, 1, 1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 1],
                [0, 0, 0, 1, 0, 0, 0],
            ]
        );

        let mut img = Array2::<u8>::zeros((9, 9));
        for y in 2..7 {
            for x in 2..7 {
                img[[y, x]] = 255;
            }
        }
        img[[1, 1]] = 255; // single pixel speck inside frame
        let rect = get_structuring_element(MorphShape::Rect, 2, 2).unwrap();
        let opened = morphology_ex_u8(img.view(), MorphOp::Open, rect.view()).unwrap();
        assert_eq!(opened[[1, 1]], 0);
        assert_eq!(opened[[4, 4]], 255);
    }

    #[test]
    fn diamond_poly_and_sector_rasterize() {
        let mut mask = Array2::<u8>::zeros((31, 31));
        let pts = [
            Point2i { x: 15, y: 0 },
            Point2i { x: 30, y: 15 },
            Point2i { x: 15, y: 30 },
            Point2i { x: 0, y: 15 },
        ];
        draw_filled_convex_poly_u8(&mut mask, &pts, 255);
        assert_eq!(mask[[15, 15]], 255);
        assert_eq!(mask[[0, 0]], 0);

        let mut cone = Array2::<u8>::from_elem((41, 41), 255);
        draw_filled_ellipse_sector_u8(&mut cone, 20.0, 20.0, 41.0, 41.0, -45.0, 45.0, 0);
        assert_eq!(cone[[20, 35]], 0); // +X inside [-45, 45]
        assert_eq!(cone[[20, 5]], 255); // -X outside
    }
}
