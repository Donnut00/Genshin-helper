//! `DiagnosticManager` implementing `gh_core::traits::DiagnosticService`
//! (UID Gaussian blur redaction, game+browser capture, `03_report.json`, `04_cv_annotated.png`, `save_user_note`).

use gh_core::atomic_io::{write_json_atomic, write_str_atomic};
use gh_core::config::AppConfig;
use gh_core::frame::{FrameBuf, RectI32};
use gh_core::i18n::get_language;
use gh_core::paths::{AppPaths, RuntimeMode};
use gh_core::traits::DiagnosticService;
use gh_core::{APP_VERSION, CoreError, CoreResult};
use gh_cv::image_io::write_image_frame;
use gh_cv::locator::MapLocator;
use gh_cv::ops::color::frame_view_to_array3;
use gh_cv::ops::filters::gaussian_blur_u8_hwc;
use gh_hud::diag_annotate::render_annotated_frame;
use gh_hud::fonts::HudFonts;
use gh_win32::capture::{ScreenCapturer, WindowFinder};
use ndarray::s;
use serde_json::{Map, Value, json};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use windows::Win32::System::SystemInformation::GetLocalTime;

pub const DEFAULT_WINDOW_TITLES: [&str; 2] = ["Genshin Impact", "原神"];

pub fn local_timestamp_compact_and_iso() -> (String, String) {
    // SAFETY: `GetLocalTime` writes a stack-allocated `SYSTEMTIME` struct.
    let st = unsafe { GetLocalTime() };
    let compact = format!(
        "{:04}{:02}{:02}_{:02}{:02}{:02}",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond
    );
    let iso = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds
    );
    (compact, iso)
}

pub type BrowserStateReader = Arc<dyn Fn() -> Map<String, Value> + Send + Sync>;
pub type NavStateReader = Arc<dyn Fn() -> Option<Value> + Send + Sync>;
pub type GameRectReader = Arc<dyn Fn() -> Option<RectI32> + Send + Sync>;

pub struct DiagnosticManager {
    paths: AppPaths,
    capturer: Arc<ScreenCapturer>,
    locator: Option<Arc<MapLocator>>,
    config: Arc<RwLock<AppConfig>>,
    read_browser_state: BrowserStateReader,
    read_nav_state: NavStateReader,
    read_game_rect: GameRectReader,
    fonts: Mutex<HudFonts>,
}

impl DiagnosticManager {
    pub fn new(
        paths: AppPaths,
        capturer: Arc<ScreenCapturer>,
        locator: Option<Arc<MapLocator>>,
        config: Arc<RwLock<AppConfig>>,
        read_browser_state: BrowserStateReader,
        read_nav_state: NavStateReader,
        read_game_rect: GameRectReader,
    ) -> Self {
        Self {
            paths,
            capturer,
            locator,
            config,
            read_browser_state,
            read_nav_state,
            read_game_rect,
            fonts: Mutex::new(HudFonts::new()),
        }
    }

    pub fn diagnostics_root(&self) -> &Path {
        &self.paths.diagnostics_dir
    }

    fn ensure_diagnostics_dir(&self) -> CoreResult<&Path> {
        std::fs::create_dir_all(&self.paths.diagnostics_dir)?;
        if self.paths.mode == RuntimeMode::Dev
            && let Some(parent) = self.paths.diagnostics_dir.parent()
        {
            let gitignore = parent.join(".gitignore");
            if !gitignore.exists() {
                let _ = write_str_atomic(&gitignore, "*\n");
            }
        }
        Ok(&self.paths.diagnostics_dir)
    }

    /// Applies strong Gaussian blur over the bottom-right UID watermark area
    /// (`x: 84%..100%`, `y: 95.5%..100%`) without mutating the input `FrameBuf`.
    pub fn redact_uid_region(frame: &FrameBuf) -> FrameBuf {
        if frame.data.is_empty() || frame.width < 16 || frame.height < 16 {
            return frame.clone();
        }
        let Ok(mut arr) = frame_view_to_array3(frame.as_view()) else {
            return frame.clone();
        };
        let (h, w, ch) = arr.dim();
        if h < 16 || w < 16 || ch == 0 {
            return frame.clone();
        }
        let x1 = ((w as f64 * 0.84).floor() as usize).min(w - 1);
        let y1 = ((h as f64 * 0.955).floor() as usize).min(h - 1);
        if x1 >= w || y1 >= h {
            return frame.clone();
        }

        let kx = (((((w - x1) as f64 * 0.25).round() as usize) | 1).clamp(15, 51)) | 1;
        let ky = (((((h - y1) as f64 * 0.65).round() as usize) | 1).clamp(15, 51)) | 1;

        let roi = arr.slice(s![y1..h, x1..w, ..]);
        if let Ok(blurred) = gaussian_blur_u8_hwc(roi, kx, ky, 0.0, 0.0) {
            arr.slice_mut(s![y1..h, x1..w, ..]).assign(&blurred);
        } else {
            return frame.clone();
        }

        let (data, _offset) = arr.into_raw_vec_and_offset();
        FrameBuf {
            width: w as u32,
            height: h as u32,
            stride: w * ch,
            format: frame.format,
            data,
        }
    }

    pub fn capture_and_create_bundle(
        &self,
        game_rect_override: Option<RectI32>,
        browser_state_override: Option<Map<String, Value>>,
        trigger_source: &str,
        user_note: &str,
        redact_uid: bool,
        nav_state_override: Option<Value>,
    ) -> CoreResult<Value> {
        let diag_root = self.ensure_diagnostics_dir()?;
        let (compact_ts, iso_ts) = local_timestamp_compact_and_iso();

        let mut folder_name = format!("diag_{compact_ts}");
        let mut bundle_dir = diag_root.join(&folder_name);
        if bundle_dir.exists() {
            for idx in 1..=99u32 {
                let candidate_name = format!("diag_{compact_ts}_{idx:02}");
                let candidate_dir = diag_root.join(&candidate_name);
                if !candidate_dir.exists() {
                    folder_name = candidate_name;
                    bundle_dir = candidate_dir;
                    break;
                }
            }
        }
        std::fs::create_dir_all(&bundle_dir)?;

        // Step 1: Capture Game Window (or fallback to primary screen)
        let mut game_rect = game_rect_override.or_else(|| (self.read_game_rect)());
        let mut game_win_info: Option<Value> = None;
        if game_rect.is_none_or(|r| r.width < 800 || r.height < 500) {
            if let Some((hwnd, l, t, w, h)) =
                WindowFinder::get_game_window(&DEFAULT_WINDOW_TITLES, false)
            {
                let rect = RectI32 {
                    x: l,
                    y: t,
                    width: w,
                    height: h,
                };
                game_win_info = Some(json!({
                    "hwnd": hwnd,
                    "rect": [l, t, w, h],
                }));
                game_rect = Some(rect);
            }
        } else if let Some(r) = game_rect {
            game_win_info = Some(json!({
                "hwnd": Value::Null,
                "rect": [r.x, r.y, r.width, r.height],
            }));
        }

        let mut game_frame = game_rect
            .and_then(|r| self.capturer.capture_region(r.x, r.y, r.width, r.height));
        if game_frame.is_none() {
            let (sw, sh) = WindowFinder::get_screen_dimensions();
            game_frame = self.capturer.capture_region(0, 0, sw, sh);
            game_win_info = Some(json!({
                "hwnd": 0,
                "rect": [0, 0, sw, sh],
                "is_fullscreen_fallback": true,
            }));
        }

        // Step 2: Capture Browser Window (or fallback to primary screen)
        let mut browser_frame: Option<FrameBuf> = None;
        let mut browser_win_info: Option<Value> = None;
        if let Some((b_hwnd, bl, bt, bw, bh)) = WindowFinder::get_browser_window() {
            browser_win_info = Some(json!({
                "hwnd": b_hwnd,
                "rect": [bl, bt, bw, bh],
                "found_browser_window": true,
            }));
            browser_frame = self.capturer.capture_region(bl, bt, bw, bh);
        }
        if browser_frame.is_none() {
            let (sw, sh) = WindowFinder::get_screen_dimensions();
            browser_frame = self.capturer.capture_region(0, 0, sw, sh);
            browser_win_info = Some(json!({
                "hwnd": 0,
                "rect": [0, 0, sw, sh],
                "found_browser_window": false,
                "is_fullscreen_fallback": true,
            }));
        }

        self.create_bundle_in_dir(
            &bundle_dir,
            &folder_name,
            &iso_ts,
            game_frame.as_ref(),
            game_win_info,
            browser_frame.as_ref(),
            browser_win_info,
            browser_state_override,
            trigger_source,
            user_note,
            redact_uid,
            nav_state_override,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn create_bundle_with_frames(
        &self,
        game_frame: Option<&FrameBuf>,
        browser_frame: Option<&FrameBuf>,
        trigger_source: &str,
        user_note: &str,
        redact_uid: bool,
        nav_state_override: Option<Value>,
    ) -> CoreResult<Value> {
        let diag_root = self.ensure_diagnostics_dir()?;
        let (compact_ts, iso_ts) = local_timestamp_compact_and_iso();
        let mut folder_name = format!("diag_{compact_ts}");
        let mut bundle_dir = diag_root.join(&folder_name);
        if bundle_dir.exists() {
            for idx in 1..=99u32 {
                let candidate_name = format!("diag_{compact_ts}_{idx:02}");
                let candidate_dir = diag_root.join(&candidate_name);
                if !candidate_dir.exists() {
                    folder_name = candidate_name;
                    bundle_dir = candidate_dir;
                    break;
                }
            }
        }
        std::fs::create_dir_all(&bundle_dir)?;
        let game_win = game_frame.map(|f| json!({"hwnd": 0, "rect": [0, 0, f.width, f.height]}));
        let browser_win =
            browser_frame.map(|f| json!({"hwnd": 0, "rect": [0, 0, f.width, f.height]}));
        self.create_bundle_in_dir(
            &bundle_dir,
            &folder_name,
            &iso_ts,
            game_frame,
            game_win,
            browser_frame,
            browser_win,
            None,
            trigger_source,
            user_note,
            redact_uid,
            nav_state_override,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn create_bundle_in_dir(
        &self,
        bundle_dir: &Path,
        folder_name: &str,
        iso_ts: &str,
        game_frame: Option<&FrameBuf>,
        game_win_info: Option<Value>,
        browser_frame: Option<&FrameBuf>,
        browser_win_info: Option<Value>,
        browser_state_override: Option<Map<String, Value>>,
        trigger_source: &str,
        user_note: &str,
        redact_uid: bool,
        nav_state_override: Option<Value>,
    ) -> CoreResult<Value> {
        let note_clean = user_note.trim().to_owned();

        // Save 01_game.png (UID-redacted if enabled)
        let mut game_written = false;
        let game_img_path = bundle_dir.join("01_game.png");
        if let Some(gf) = game_frame {
            let res = if redact_uid {
                let redacted = Self::redact_uid_region(gf);
                write_image_frame(&game_img_path, redacted.as_view(), Some("png"))
            } else {
                write_image_frame(&game_img_path, gf.as_view(), Some("png"))
            };
            game_written = res.is_ok();
        }

        // Save 02_browser.png
        let mut browser_written = false;
        let browser_img_path = bundle_dir.join("02_browser.png");
        if let Some(bf) = browser_frame {
            browser_written = write_image_frame(&browser_img_path, bf.as_view(), Some("png")).is_ok();
        }

        // Step 3: Run CV localization on UNREDACTED game_frame
        let cv_telemetry: Value = if let Some(gf) = game_frame {
            if let Some(loc) = &self.locator {
                loc.locate_player_frame(gf.as_view(), true, None)
                    .map(|r| r.to_json_value())
                    .unwrap_or_else(|| json!({"detected": false, "has_world_coords": false}))
            } else {
                json!({
                    "detected": false,
                    "has_world_coords": false,
                    "reason": "Locator unavailable"
                })
            }
        } else {
            json!({
                "detected": false,
                "has_world_coords": false,
                "reason": "Capture failed"
            })
        };

        // Step 4: Render annotated visual (04_cv_annotated.png), THEN redact UID
        let mut annotated_written = false;
        let annotated_path = bundle_dir.join("04_cv_annotated.png");
        if let Some(gf) = game_frame {
            let annotated_res = {
                let mut fonts = self.fonts.lock().unwrap_or_else(|e| e.into_inner());
                render_annotated_frame(&mut fonts, gf.as_view(), &cv_telemetry)
            };
            match annotated_res {
                Ok(ann) => {
                    let final_ann = if redact_uid {
                        Self::redact_uid_region(&ann)
                    } else {
                        ann
                    };
                    annotated_written =
                        write_image_frame(&annotated_path, final_ann.as_view(), Some("png"))
                            .is_ok();
                }
                Err(e) => {
                    tracing::warn!("Diagnostic annotation render failed: {e}");
                    let fallback = if redact_uid {
                        Self::redact_uid_region(gf)
                    } else {
                        gf.clone()
                    };
                    annotated_written =
                        write_image_frame(&annotated_path, fallback.as_view(), Some("png")).is_ok();
                }
            }
        }

        // Step 5: Write 03_report.json and optional 05_user_note.txt
        let browser_state = browser_state_override.unwrap_or_else(|| (self.read_browser_state)());
        let nav_state = nav_state_override
            .or_else(|| (self.read_nav_state)())
            .unwrap_or_else(|| Value::Object(Map::new()));
        let cfg_snap = self.config.read().expect("config lock poisoned").clone();
        let exe_str = std::env::current_exe()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();

        let report_data = json!({
            "timestamp": iso_ts,
            "trigger_source": trigger_source,
            "user_note": note_clean,
            "uid_redacted": redact_uid,
            "game_window": game_win_info,
            "browser_window": browser_win_info,
            "browser_state_from_web": browser_state,
            "cv_telemetry": cv_telemetry,
            "nav_state": nav_state,
            "config": {
                "hotkey_sync": cfg_snap.hotkey_sync,
                "hotkey_debug": cfg_snap.hotkey_debug,
                "chest_search_radius": cfg_snap.chest_search_radius,
                "ws_port": cfg_snap.ws_port,
            },
            "system": {
                "platform": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
                "backend": "rust",
                "app_version": APP_VERSION,
                "executable": exe_str,
            }
        });
        let report_path = bundle_dir.join("03_report.json");
        let report_written = write_json_atomic(&report_path, &report_data).is_ok();

        let mut files_list = Vec::new();
        if game_written {
            files_list.push("01_game.png".to_owned());
        }
        if browser_written {
            files_list.push("02_browser.png".to_owned());
        }
        if report_written {
            files_list.push("03_report.json".to_owned());
        }
        if annotated_written {
            files_list.push("04_cv_annotated.png".to_owned());
        }
        if !note_clean.is_empty() {
            let note_path = bundle_dir.join("05_user_note.txt");
            if write_str_atomic(&note_path, &format!("{note_clean}\n")).is_ok() {
                files_list.push("05_user_note.txt".to_owned());
            }
        }

        let default_reason = if get_language() == "vi" {
            "Đã chụp chẩn đoán thành công."
        } else {
            "Captured successfully"
        };
        let reason = cv_telemetry
            .get("reason")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(default_reason)
            .to_owned();
        let has_world_coords = cv_telemetry
            .get("has_world_coords")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        Ok(json!({
            "success": true,
            "bundle_dir": bundle_dir.to_string_lossy().into_owned(),
            "folder_name": folder_name,
            "has_world_coords": has_world_coords,
            "reason": reason,
            "user_note": note_clean,
            "files": files_list,
        }))
    }

    pub fn save_user_note(&self, folder_name: &str, note: &str) -> CoreResult<Value> {
        let clean = folder_name.trim();
        if clean.is_empty()
            || !clean.starts_with("diag_")
            || clean.contains("..")
            || clean.contains('/')
            || clean.contains('\\')
            || Path::new(clean).file_name().and_then(|s| s.to_str()) != Some(clean)
        {
            return Err(CoreError::InvalidArgument(
                "Invalid diagnostic folder name".to_owned(),
            ));
        }

        let diag_root = self.ensure_diagnostics_dir()?.to_path_buf();
        let bundle_dir = diag_root.join(clean);
        if !bundle_dir.is_dir() {
            return Err(CoreError::NotFound(format!(
                "Diagnostic folder not found: {clean}"
            )));
        }

        let canon_root = std::fs::canonicalize(&diag_root)?;
        let canon_bundle = std::fs::canonicalize(&bundle_dir)?;
        if !canon_bundle.starts_with(&canon_root) {
            return Err(CoreError::InvalidArgument(
                "Invalid diagnostic folder path".to_owned(),
            ));
        }

        let note_clean = note.trim().to_owned();
        let note_path = bundle_dir.join("05_user_note.txt");
        let content = if note_clean.is_empty() {
            String::new()
        } else {
            format!("{note_clean}\n")
        };
        write_str_atomic(&note_path, &content)?;

        let report_path = bundle_dir.join("03_report.json");
        if report_path.is_file()
            && let Ok(raw) = std::fs::read_to_string(&report_path)
            && let Ok(Value::Object(mut obj)) = serde_json::from_str::<Value>(&raw)
        {
            let (_, iso_ts) = local_timestamp_compact_and_iso();
            obj.insert("user_note".to_owned(), Value::String(note_clean.clone()));
            obj.insert("user_note_updated_at".to_owned(), Value::String(iso_ts));
            let _ = write_json_atomic(&report_path, &Value::Object(obj));
        }

        Ok(json!({
            "ok": true,
            "folder_name": clean,
            "note_path": note_path.to_string_lossy().into_owned(),
            "user_note": note_clean,
        }))
    }
}

impl DiagnosticService for DiagnosticManager {
    fn trigger_diagnostic(&self, note: Option<&str>, source: Option<&str>) -> CoreResult<Value> {
        let src = source
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("web_ui");
        self.capture_and_create_bundle(None, None, src, note.unwrap_or(""), true, None)
    }

    fn save_diagnostic_note(&self, folder_name: &str, note: &str) -> CoreResult<Value> {
        self.save_user_note(folder_name, note)
    }
}

pub fn ensure_writable_parent(path: &Path) -> CoreResult<PathBuf> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent)?;
    }
    Ok(path.to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;
    use gh_core::frame::PixelFormat;
    use gh_cv::image_io::read_image_bgr;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static DIAG_TEST_SEQ: AtomicUsize = AtomicUsize::new(0);

    fn temp_diag_paths(tag: &str) -> (PathBuf, AppPaths) {
        let seq = DIAG_TEST_SEQ.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "gh_app_diag_{tag}_{}_{seq}_tiếng_việt",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let paths = AppPaths {
            mode: RuntimeMode::Installed,
            config_path: root.join("config.json"),
            diagnostics_dir: root.join("diagnostics"),
            data_dir: root.join("data"),
            tiles_dir: root.join("tiles"),
            ug_overlays_dir: root.join("ug_overlays"),
            cache_dir: root.join("cache"),
            dev_data_dir: None,
        };
        (root, paths)
    }

    fn roi_gradient_energy(frame: &FrameBuf, x1: usize, y1: usize) -> f64 {
        let w = frame.width as usize;
        let h = frame.height as usize;
        let ch = frame.format.channels();
        let mut sum = 0.0f64;
        let mut count = 0usize;
        for y in (y1 + 1)..(h - 1) {
            for x in (x1 + 1)..(w - 1) {
                let c = frame.data[y * frame.stride + x * ch] as f64;
                let r = frame.data[y * frame.stride + (x + 1) * ch] as f64;
                let d = frame.data[(y + 1) * frame.stride + x * ch] as f64;
                sum += (r - c).abs() + (d - c).abs();
                count += 1;
            }
        }
        if count == 0 { 0.0 } else { sum / count as f64 }
    }

    fn make_checkerboard_bgr(w: u32, h: u32) -> FrameBuf {
        let mut fb = FrameBuf::new_zeroed(w, h, PixelFormat::Bgr8);
        let x1 = ((w as f64 * 0.84).floor() as usize).min(w as usize - 1);
        let y1 = ((h as f64 * 0.955).floor() as usize).min(h as usize - 1);
        for y in 0..(h as usize) {
            for x in 0..(w as usize) {
                let idx = y * fb.stride + x * 3;
                if x >= x1 && y >= y1 {
                    let val = if ((x + y) % 2) == 0 { 255 } else { 0 };
                    fb.data[idx] = val;
                    fb.data[idx + 1] = val;
                    fb.data[idx + 2] = val;
                } else if x < 32 && y < 32 {
                    fb.data[idx] = 123;
                    fb.data[idx + 1] = 45;
                    fb.data[idx + 2] = 210;
                } else {
                    fb.data[idx] = 180;
                    fb.data[idx + 1] = 180;
                    fb.data[idx + 2] = 180;
                }
            }
        }
        fb
    }

    #[test]
    fn redact_uid_region_blurs_roi_without_mutating_input_or_outside_pixels() {
        let tiny = FrameBuf::new_zeroed(12, 12, PixelFormat::Bgr8);
        assert_eq!(DiagnosticManager::redact_uid_region(&tiny).data, tiny.data);

        let src = make_checkerboard_bgr(1280, 720);
        let snap_top_left = src.data[0..9].to_vec();
        let x1 = (1280.0 * 0.84_f64).floor() as usize;
        let y1 = (720.0 * 0.955_f64).floor() as usize;
        let before_energy = roi_gradient_energy(&src, x1, y1);
        assert!(before_energy > 200.0);

        let redacted = DiagnosticManager::redact_uid_region(&src);
        // Input unchanged
        assert_eq!(src.data[0..9], snap_top_left);
        // Outside ROI unchanged
        assert_eq!(redacted.data[0..9], snap_top_left);
        // Bottom-right UID ROI blurred by >90%
        let after_energy = roi_gradient_energy(&redacted, x1, y1);
        assert!(
            after_energy < before_energy * 0.10,
            "expected >90% reduction, before={before_energy}, after={after_energy}"
        );
    }

    #[test]
    fn bundle_creation_annotates_redacts_and_guards_save_user_note() {
        let (root, paths) = temp_diag_paths("bundle");
        let mgr = DiagnosticManager::new(
            paths,
            Arc::new(ScreenCapturer::new(2)),
            None,
            Arc::new(RwLock::new(AppConfig::default())),
            Arc::new(Map::new),
            Arc::new(|| None),
            Arc::new(|| None),
        );

        let gf = make_checkerboard_bgr(1280, 720);
        let bf = FrameBuf::new_zeroed(320, 240, PixelFormat::Bgr8);
        let res = mgr
            .create_bundle_with_frames(
                Some(&gf),
                Some(&bf),
                "unit_test",
                "Ghi chú tiếng Việt kiểm tra",
                true,
                Some(json!({"state": "idle"})),
            )
            .unwrap();

        assert_eq!(res["success"], Value::Bool(true));
        let folder = res["folder_name"].as_str().unwrap().to_owned();
        let bdir = PathBuf::from(res["bundle_dir"].as_str().unwrap());
        assert!(bdir.join("01_game.png").is_file());
        assert!(bdir.join("02_browser.png").is_file());
        assert!(bdir.join("03_report.json").is_file());
        assert!(bdir.join("04_cv_annotated.png").is_file());
        assert!(bdir.join("05_user_note.txt").is_file());

        let report_raw = std::fs::read_to_string(bdir.join("03_report.json")).unwrap();
        let report: Value = serde_json::from_str(&report_raw).unwrap();
        assert_eq!(report["uid_redacted"], Value::Bool(true));
        assert_eq!(report["user_note"], Value::String("Ghi chú tiếng Việt kiểm tra".to_owned()));

        // Verify saved 04_cv_annotated.png also has UID ROI blurred (<10% sharpness)
        let ann_img = read_image_bgr(&bdir.join("04_cv_annotated.png")).unwrap();
        let x1 = (1280.0 * 0.84_f64).floor() as usize;
        let y1 = (720.0 * 0.955_f64).floor() as usize;
        let mut ann_grad = 0.0f64;
        let mut cnt = 0usize;
        for y in (y1 + 1)..719 {
            for x in (x1 + 1)..1279 {
                let c = ann_img[[y, x, 0]] as f64;
                let r = ann_img[[y, x + 1, 0]] as f64;
                let d = ann_img[[y + 1, x, 0]] as f64;
                ann_grad += (r - c).abs() + (d - c).abs();
                cnt += 1;
            }
        }
        let ann_energy = ann_grad / cnt.max(1) as f64;
        assert!(
            ann_energy < 20.0,
            "annotated ROI should be strongly blurred, got {ann_energy}"
        );

        // Traversal guard
        for bad in ["../bad", "diag_a/../b", "diag_a/b", "diag_a\\b", "foo", ""] {
            assert!(matches!(
                mgr.save_user_note(bad, "test"),
                Err(CoreError::InvalidArgument(_))
            ));
        }
        // Missing bundle -> NotFound
        assert!(matches!(
            mgr.save_user_note("diag_99991231_235959", "test"),
            Err(CoreError::NotFound(_))
        ));
        // Valid update -> updates 05_user_note.txt & 03_report.json
        let upd = mgr.save_user_note(&folder, "Cập nhật tiếng Việt mới").unwrap();
        assert_eq!(upd["ok"], Value::Bool(true));
        let note_txt = std::fs::read_to_string(bdir.join("05_user_note.txt")).unwrap();
        assert_eq!(note_txt.trim(), "Cập nhật tiếng Việt mới");
        let report_after: Value =
            serde_json::from_str(&std::fs::read_to_string(bdir.join("03_report.json")).unwrap())
                .unwrap();
        assert_eq!(
            report_after["user_note"],
            Value::String("Cập nhật tiếng Việt mới".to_owned())
        );

        // Missing frames must omit unwritten PNG names from `files`
        let partial_res = mgr
            .create_bundle_with_frames(None, None, "unit_test_empty", "", false, None)
            .unwrap();
        let files = partial_res["files"].as_array().unwrap();
        let file_names: Vec<&str> = files.iter().filter_map(Value::as_str).collect();
        assert_eq!(file_names, vec!["03_report.json"]);

        let _ = std::fs::remove_dir_all(&root);
    }
}
