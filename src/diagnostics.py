import datetime
import json
import logging
import os
import platform
import subprocess
import sys
import unicodedata
from typing import Optional, Dict, Any, Tuple
import cv2
import numpy as np

from src.capture import WindowFinder, ScreenCapturer
from src.config import config, CONFIG_FILE_PATH
from src.i18n import get_language
from src.image_io import write_image, write_json_atomic
from src.locator import MapLocator
from src.paths import get_diagnostics_dir, is_frozen

logger = logging.getLogger("GenshinHelper.Diagnostics")


class DiagnosticManager:
    """
    Diagnostic Capture Engine:
    1. Simultaneously grabs Game Window and Browser (HoYoLAB Web Map) Window.
    2. Runs Computer Vision localization in deep-debug mode.
    3. Generates high-fidelity annotated diagnostic visual overlay (04_cv_annotated.png).
    4. Bundles all machine-readable telemetry into a structured JSON report (03_report.json).
    5. Saves all assets in scratch/diagnostics/diag_YYYYMMDD_HHMMSS/.
    6. Supports saving user bug notes (05_user_note.txt) via browser note modal.
    """

    @staticmethod
    def _ensure_scratch_dir() -> str:
        diag_dir = get_diagnostics_dir()
        os.makedirs(diag_dir, exist_ok=True)

        # Compliance with Rule 4: scratch/.gitignore containing * in dev workspace
        if not is_frozen():
            scratch_dir = os.path.dirname(os.path.abspath(diag_dir))
            git_ignore_path = os.path.join(scratch_dir, ".gitignore")
            if not os.path.exists(git_ignore_path):
                try:
                    with open(git_ignore_path, "w", encoding="utf-8") as f:
                        f.write("*\n")
                except Exception:
                    pass

        return diag_dir

    @staticmethod
    def redact_uid_region(frame: np.ndarray) -> np.ndarray:
        """
        Applies strong Gaussian blur over the bottom-right UID watermark area
        (x: 84%-100%, y: 95.5%-100%) without mutating the input array.
        """
        if frame is None or frame.size == 0 or frame.ndim < 2:
            return frame
        redacted = frame.copy()
        h, w = redacted.shape[:2]
        if h < 16 or w < 16:
            return redacted
        x1 = max(0, min(w - 1, int(w * 0.84)))
        y1 = max(0, min(h - 1, int(h * 0.955)))
        roi = redacted[y1:h, x1:w]
        if roi.size == 0:
            return redacted
        kx = min(51, max(15, int(round((w - x1) * 0.25)) | 1)) | 1
        ky = min(51, max(15, int(round((h - y1) * 0.65)) | 1)) | 1
        redacted[y1:h, x1:w] = cv2.GaussianBlur(roi, (kx, ky), 0)
        return redacted

    @classmethod
    def capture_and_create_bundle(
        cls,
        capturer: ScreenCapturer,
        locator: MapLocator,
        game_rect: Optional[Tuple[int, int, int, int]] = None,
        browser_state: Optional[Dict[str, Any]] = None,
        trigger_source: str = "hotkey_debug",
        user_note: str = "",
        redact_uid: bool = True,
        nav_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes complete diagnostic pipeline and packages all files.
        """
        now = datetime.datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        folder_name = f"diag_{timestamp_str}"
        diag_root = cls._ensure_scratch_dir()
        bundle_dir = os.path.join(diag_root, folder_name)
        os.makedirs(bundle_dir, exist_ok=True)
        note_clean = (user_note or "").strip() if isinstance(user_note, str) else ""

        logger.info(f"📸 Starting Diagnostic Capture & Bundle -> {bundle_dir}...")

        # -------------------------------------------------------------
        # Step 1: Capture Game Window
        # -------------------------------------------------------------
        game_win_info = None
        if not game_rect or game_rect[2] < 800 or game_rect[3] < 500:
            found = WindowFinder.get_game_window(config.WINDOW_TITLES)
            if found:
                game_win_info = {
                    "hwnd": found[0],
                    "rect": (found[1], found[2], found[3], found[4])
                }
                game_rect = (found[1], found[2], found[3], found[4])
        else:
            game_win_info = {"hwnd": None, "rect": game_rect}

        game_frame = None
        if game_rect:
            game_frame = capturer.capture_region(*game_rect)

        # Fallback to full primary screen if game capture failed
        if game_frame is None:
            sw, sh = WindowFinder.get_screen_dimensions()
            game_frame = capturer.capture_region(0, 0, sw, sh)
            game_win_info = {"hwnd": 0, "rect": (0, 0, sw, sh), "is_fullscreen_fallback": True}

        game_img_path = os.path.join(bundle_dir, "01_game.png")
        if game_frame is not None:
            game_to_save = cls.redact_uid_region(game_frame) if redact_uid else game_frame
            write_image(game_img_path, game_to_save)
            logger.info(f"  [1/4] Saved game screenshot: {game_img_path}")
        else:
            logger.warning("  [1/4] Failed to capture game screenshot.")

        # -------------------------------------------------------------
        # Step 2: Capture Browser Window (HoYoLAB Web Map)
        # -------------------------------------------------------------
        browser_win_info = None
        browser_frame = None
        b_win = WindowFinder.get_browser_window()
        if b_win:
            browser_win_info = {
                "hwnd": b_win[0],
                "rect": (b_win[1], b_win[2], b_win[3], b_win[4]),
                "found_browser_window": True
            }
            browser_frame = capturer.capture_region(b_win[1], b_win[2], b_win[3], b_win[4])

        if browser_frame is None:
            sw, sh = WindowFinder.get_screen_dimensions()
            browser_frame = capturer.capture_region(0, 0, sw, sh)
            browser_win_info = {
                "hwnd": 0,
                "rect": (0, 0, sw, sh),
                "found_browser_window": False,
                "is_fullscreen_fallback": True
            }

        browser_img_path = os.path.join(bundle_dir, "02_browser.png")
        if browser_frame is not None:
            write_image(browser_img_path, browser_frame)
            logger.info(f"  [2/4] Saved browser screenshot: {browser_img_path}")

        # -------------------------------------------------------------
        # Step 3: Run Deep Computer Vision Diagnostics
        # -------------------------------------------------------------
        cv_telemetry = {}
        if game_frame is not None:
            cv_telemetry = locator.locate_player(game_frame, debug=True) or {}
            logger.info(f"  [3/4] CV Localization: detected={cv_telemetry.get('detected')}, has_world={cv_telemetry.get('has_world_coords')}")

        # -------------------------------------------------------------
        # Step 4: Render Annotated CV Visual (04_cv_annotated.png)
        # -------------------------------------------------------------
        annotated_path = os.path.join(bundle_dir, "04_cv_annotated.png")
        if game_frame is not None:
            annotated_frame = cls._render_annotated_image(game_frame, cv_telemetry)
            if redact_uid:
                annotated_frame = cls.redact_uid_region(annotated_frame)
            write_image(annotated_path, annotated_frame)
            logger.info(f"  [4/4] Saved annotated visual: {annotated_path}")

        # -------------------------------------------------------------
        # Step 5: Save Telemetry JSON Report (03_report.json)
        # -------------------------------------------------------------
        report_data = {
            "timestamp": now.isoformat(),
            "trigger_source": trigger_source,
            "user_note": note_clean,
            "uid_redacted": bool(redact_uid),
            "game_window": game_win_info,
            "browser_window": browser_win_info,
            "browser_state_from_web": browser_state or {},
            "cv_telemetry": cv_telemetry,
            # In-game Mini-HUD state at capture time (the HUD itself is excluded from capture)
            "nav_state": nav_state or {},
            "config": {
                "hotkey_sync": config.HOTKEY_MARK,
                "hotkey_debug": config.HOTKEY_DEBUG,
                "chest_search_radius": config.CHEST_SEARCH_RADIUS,
                "ws_port": config.WS_PORT,
            },
            "system": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "executable": sys.executable
            }
        }

        report_path = os.path.join(bundle_dir, "03_report.json")
        try:
            write_json_atomic(report_path, report_data, indent=2, ensure_ascii=False)
            logger.info(f"  Saved JSON report: {report_path}")
        except Exception as e:
            logger.error(f"Failed to write report.json: {e}")

        files_list = [
            "01_game.png",
            "02_browser.png",
            "03_report.json",
            "04_cv_annotated.png",
        ]
        if note_clean:
            note_path = os.path.join(bundle_dir, "05_user_note.txt")
            try:
                with open(note_path, "w", encoding="utf-8") as nf:
                    nf.write(note_clean + "\n")
                files_list.append("05_user_note.txt")
            except Exception as e:
                logger.error(f"Failed to write user note: {e}")

        logger.info(f"==> Saved diagnostic bundle: {bundle_dir}")

        default_reason = (
            "Đã chụp chẩn đoán thành công."
            if get_language() == "vi"
            else "Captured successfully"
        )
        return {
            "success": True,
            "bundle_dir": bundle_dir,
            "folder_name": folder_name,
            "has_world_coords": cv_telemetry.get("has_world_coords", False),
            "reason": cv_telemetry.get("reason", default_reason),
            "user_note": note_clean,
            "files": files_list,
        }

    @classmethod
    def save_user_note(cls, folder_name: str, note: str) -> Dict[str, Any]:
        """Saves or updates 05_user_note.txt and 03_report.json inside a diagnostic bundle."""
        if not isinstance(folder_name, str):
            return {"ok": False, "error": "Invalid diagnostic folder name"}
        clean_name = folder_name.strip()
        if (
            not clean_name.startswith("diag_")
            or os.path.basename(clean_name) != clean_name
            or ".." in clean_name
            or "/" in clean_name
            or "\\" in clean_name
        ):
            return {"ok": False, "error": "Invalid diagnostic folder name"}

        diag_root = os.path.abspath(cls._ensure_scratch_dir())
        bundle_dir = os.path.abspath(os.path.join(diag_root, clean_name))
        if not bundle_dir.startswith(diag_root + os.sep) or not os.path.isdir(bundle_dir):
            return {"ok": False, "not_found": True, "error": f"Diagnostic folder not found: {clean_name}"}

        note_clean = (note or "").strip() if isinstance(note, str) else ""
        note_path = os.path.join(bundle_dir, "05_user_note.txt")
        try:
            with open(note_path, "w", encoding="utf-8") as nf:
                nf.write(note_clean + ("\n" if note_clean else ""))
        except Exception as e:
            return {"ok": False, "error": f"Failed to write user note: {e}"}

        report_path = os.path.join(bundle_dir, "03_report.json")
        if os.path.isfile(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as rf:
                    report_data = json.load(rf)
                if isinstance(report_data, dict):
                    report_data["user_note"] = note_clean
                    report_data["user_note_updated_at"] = datetime.datetime.now().isoformat()
                    write_json_atomic(report_path, report_data, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed updating report.json user_note: {e}")

        return {
            "ok": True,
            "folder_name": clean_name,
            "note_path": note_path,
            "user_note": note_clean,
        }

    @staticmethod
    def _ascii_safe(text: Any) -> str:
        """Transliterates Unicode text to ASCII so OpenCV putText never renders ???."""
        if text is None:
            return ""
        s = str(text).replace("đ", "d").replace("Đ", "D")
        norm = unicodedata.normalize("NFKD", s)
        ascii_chars = [c for c in norm if not unicodedata.combining(c) and ord(c) < 128]
        return "".join(ascii_chars)

    @staticmethod
    def _render_annotated_image(frame: np.ndarray, telemetry: Dict[str, Any]) -> np.ndarray:
        """
        Draws visual CV debugging markers:
        - Header banner with summary and coordinates.
        - Zoom Slider bounding box, ratio t, and measured scale S.
        - Player Arrow location and orientation.
        - Waypoint candidates (orange) and matched inliers (green).
        """
        canvas = frame.copy()
        h, w = canvas.shape[:2]

        # 1. Header Banner
        banner_h = 70
        overlay = canvas.copy()
        cv2.rectangle(overlay, (0, 0), (w, banner_h), (15, 23, 42), -1)
        cv2.addWeighted(overlay, 0.85, canvas, 0.15, 0, canvas)
        cv2.line(canvas, (0, banner_h), (w, banner_h), (56, 189, 248), 2)

        has_world = telemetry.get("has_world_coords", False)
        status_color = (74, 222, 128) if has_world else (248, 113, 113)  # Green / Red
        status_text = "STATUS: SUCCESS" if has_world else "STATUS: FAILED / NO WORLD COORDS"

        cv2.putText(canvas, status_text, (20, 28), cv2.FONT_HERSHEY_DUPLEX, 0.75, status_color, 2, cv2.LINE_AA)

        info_line1 = ""
        if has_world:
            map_name = DiagnosticManager._ascii_safe(telemetry.get("map_name", "Teyvat"))
            lat = telemetry.get("lat", 0.0)
            lng = telemetry.get("lng", 0.0)
            rmse = telemetry.get("rmse", 0.0)
            inliers_cnt = telemetry.get("inliers_count", 0)
            if telemetry.get("is_underground"):
                gid = telemetry.get("group_id")
                fid = telemetry.get("floor_id")
                fshort = DiagnosticManager._ascii_safe(
                    telemetry.get("floor_short") or telemetry.get("floor_name") or f"Floor {telemetry.get('floor_idx')}"
                )
                ug_str = f" [G#{gid} Fl#{fid} {fshort}]" if gid is not None else f" [Floor {telemetry.get('floor_idx')}]"
            else:
                ug_str = " [Surface]"
            info_line1 = DiagnosticManager._ascii_safe(
                f"World: ({lat:.1f}, {lng:.1f}){ug_str} | Map: {map_name} | Inliers: {inliers_cnt} (RMSE: {rmse:.1f})"
            )
        else:
            reason = telemetry.get("reason", "Unknown")
            info_line1 = DiagnosticManager._ascii_safe(f"Reason: {reason}")

        cv2.putText(canvas, info_line1, (20, 55), cv2.FONT_HERSHEY_DUPLEX, 0.55, (226, 232, 240), 1, cv2.LINE_AA)

        # 2. Draw Zoom Slider overlay
        slider_res = telemetry.get("slider_res")
        if slider_res and slider_res.get("found"):
            box = slider_res.get("box_coords")
            t_val = slider_res.get("ratio_t", 0.0)
            scale_val = slider_res.get("scale", 0.0)
            score_val = slider_res.get("score", 0.0)

            if box and len(box) == 4:
                x1, y1, x2, y2 = box
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 230, 255), 2)
                cv2.putText(canvas, f"Zoom Slider (t={t_val*100:.1f}%, Scale={scale_val:.4f})", (x2 + 10, (y1 + y2) // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 230, 255), 2, cv2.LINE_AA)

        # 3. Draw Waypoint Candidates (Orange circles)
        candidates = telemetry.get("candidates", [])
        for c in candidates:
            cx, cy = int(c["x"]), int(c["y"])
            cv2.circle(canvas, (cx, cy), 18, (0, 140, 255), 2, cv2.LINE_AA)
            cv2.putText(canvas, "Cand", (cx - 15, cy - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 140, 255), 1, cv2.LINE_AA)

        # 4. Draw Matched Inliers (Green circles)
        inliers = telemetry.get("inliers", [])
        for inp in inliers:
            ix, iy = int(inp["pixel_x"]), int(inp["pixel_y"])
            w_id = inp.get("waypoint_id", "")
            w_name = inp.get("name", "")
            cv2.circle(canvas, (ix, iy), 22, (34, 197, 94), 3, cv2.LINE_AA)
            cv2.circle(canvas, (ix, iy), 4, (34, 197, 94), -1)
            tag = f"WP #{w_id}"
            cv2.putText(canvas, tag, (ix + 12, iy - 10), cv2.FONT_HERSHEY_DUPLEX, 0.5, (34, 197, 94), 2, cv2.LINE_AA)

        # 5. Draw Player Arrow
        px = telemetry.get("pixel_x")
        py = telemetry.get("pixel_y")
        heading = telemetry.get("heading", 0.0)
        if px is not None and py is not None:
            ipx, ipy = int(px), int(py)
            # Draw crosshair & circle
            cv2.circle(canvas, (ipx, ipy), 24, (0, 0, 255), 3, cv2.LINE_AA)
            cv2.drawMarker(canvas, (ipx, ipy), (0, 0, 255), cv2.MARKER_CROSS, 32, 2)

            # Draw heading arrow ray (0 deg = Up, clockwise)
            rad = np.radians(heading)
            dx = int(45 * np.sin(rad))
            dy = int(-45 * np.cos(rad))
            cv2.arrowedLine(canvas, (ipx, ipy), (ipx + dx, ipy + dy), (0, 0, 255), 3, tipLength=0.35)
            cv2.putText(canvas, f"PLAYER ({heading:.0f} deg)", (ipx + 28, ipy + 8),
                        cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)

        return canvas
