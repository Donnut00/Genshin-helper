import os
import sys
import math
import ctypes
import logging
from typing import Optional, Tuple
import cv2
import numpy as np

from src.heading360 import extract_camera_view_cone, extract_player_arrow_360

logger = logging.getLogger("GenshinHelper.Tracker")


class CvAutoTrackBridge:
    """
    [LEGACY / OPTIONAL] Direct ctypes bridge to external cvAutoTrack.dll.
    Retained for backwards compatibility; Genshin-Helper v5 uses pure Python/OpenCV
    heading extraction (src/heading360.py) and terrain matching (src/locator.py)
    without requiring any external DLL.
    Zero-memory reading, 100% video-based minimap tracking.
    """
    def __init__(self, dll_path: str = "cvAutoTrack.dll"):
        self.dll_path = dll_path
        self._lib = None
        self.is_loaded = False
        self._load()

    def _load(self):
        if not os.path.exists(self.dll_path):
            # Check in lib/ subfolder
            alt_path = os.path.join(os.path.dirname(__file__), "..", "lib", "cvAutoTrack.dll")
            if os.path.exists(alt_path):
                self.dll_path = alt_path
            else:
                return

        try:
            self._lib = ctypes.cdll.LoadLibrary(self.dll_path)
            # Define explicit 64-bit ctypes function signatures
            # bool init()
            self._lib.init.argtypes = []
            self._lib.init.restype = ctypes.c_bool
            # bool uninit()
            self._lib.uninit.argtypes = []
            self._lib.uninit.restype = ctypes.c_bool
            # bool GetTransform(double &x, double &y, double &a)
            self._lib.GetTransform.argtypes = [
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double),
            ]
            self._lib.GetTransform.restype = ctypes.c_bool

            if self._lib.init():
                self.is_loaded = True
                logger.info(f"Loaded legacy cvAutoTrack.dll successfully from {self.dll_path}")
            else:
                logger.warning("cvAutoTrack init() returned false.")
        except Exception as e:
            logger.warning(f"Could not load cvAutoTrack.dll: {e}")

    def get_transform(self) -> Optional[Tuple[float, float, float]]:
        if not self.is_loaded or not self._lib:
            return None
        x = ctypes.c_double(0)
        y = ctypes.c_double(0)
        a = ctypes.c_double(0)
        success = self._lib.GetTransform(ctypes.byref(x), ctypes.byref(y), ctypes.byref(a))
        if success:
            return (float(x.value), float(y.value), float(a.value))
        return None

    def close(self):
        if self.is_loaded and self._lib:
            try:
                self._lib.uninit()
            except Exception:
                pass


class PureOpenCVTracker:
    """
    Pure Python/OpenCV Tracker that identifies player position and 360-degree heading.
    Supports:
    1. Minimap 360-degree player arrow heading (3-tip contour geometry via heading360).
    2. Minimap 360-degree camera view cone heading (BGRA alpha / BGR blend / HSV fallback).
    3. Fullscreen Map (M) player marker tracking.
    """
    def __init__(self):
        # HSV bounds for the player's cyan arrow icon in Genshin Impact
        # Hue: 85 - 105, Saturation: 160 - 255, Value: 180 - 255
        self.cyan_lower = np.array([85, 160, 180], dtype=np.uint8)
        self.cyan_upper = np.array([105, 255, 255], dtype=np.uint8)

        # Yellow ping circle HSV bounds
        self.yellow_lower = np.array([20, 120, 180], dtype=np.uint8)
        self.yellow_upper = np.array([35, 255, 255], dtype=np.uint8)

        self.last_arrow_confidence: float = 0.0
        self.last_camera_confidence: float = 0.0
        self.last_camera_method: str = "none"

    def find_player_on_fullscreen_map(self, map_frame: np.ndarray) -> Optional[Tuple[float, float, float]]:
        """
        Locates the cyan player arrow on the opened full map (M).
        Returns (pixel_x, pixel_y, angle_degrees_cw_north) relative to the frame.
        """
        if map_frame is None or map_frame.size == 0:
            return None

        bgr = map_frame[:, :, :3] if map_frame.ndim == 3 and map_frame.shape[2] == 4 else map_frame
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.cyan_lower, self.cyan_upper)

        # Morphological clean up to remove noise
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # Filter contours by typical player arrow size (usually between 30 and 400 pixels in area)
        valid_contours = [c for c in contours if 20 < cv2.contourArea(c) < 600]
        if not valid_contours:
            return None

        largest = max(valid_contours, key=cv2.contourArea)
        M = cv2.moments(largest)
        if M["m00"] == 0:
            return None

        cx = float(M["m10"] / M["m00"])
        cy = float(M["m01"] / M["m00"])

        # Farthest contour vertex from centroid is the arrow tip -> 0 deg = North, CW [0, 360)
        pts = largest.reshape(-1, 2)
        dists = (pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2
        tip = pts[int(np.argmax(dists))]
        angle = float(math.degrees(math.atan2(float(tip[0]) - cx, -(float(tip[1]) - cy))) % 360.0)

        return (cx, cy, angle)

    def get_minimap_heading(self, minimap_crop: np.ndarray) -> Optional[float]:
        """
        Extracts the exact 360-degree orientation (0 deg = North, CW [0, 360))
        of the player's cyan arrow at the center of the minimap.
        """
        angle, conf = extract_player_arrow_360(minimap_crop)
        self.last_arrow_confidence = conf
        return angle

    def get_minimap_camera_heading(self, minimap_crop: np.ndarray) -> Optional[float]:
        """
        Extracts the exact 360-degree camera view cone heading (0 deg = North, CW [0, 360))
        from the minimap crop (BGRA alpha fast path -> BGR blend -> HSV adaptive fallback).
        """
        angle, conf, method = extract_camera_view_cone(minimap_crop)
        self.last_camera_confidence = conf
        self.last_camera_method = method
        return angle


class HybridTracker:
    """Unified tracker combining legacy/optional cvAutoTrack DLL with pure OpenCV fallback."""
    def __init__(self):
        self.c_tracker = CvAutoTrackBridge()
        self.py_tracker = PureOpenCVTracker()

    def track(self, frame: Optional[np.ndarray]) -> Optional[Tuple[float, float, float]]:
        # 1. Legacy/optional: cvAutoTrack C++ engine if DLL is manually provided
        if self.c_tracker.is_loaded:
            t = self.c_tracker.get_transform()
            if t is not None:
                return t

        # 2. Primary v5 pure OpenCV visual detection on frame
        if frame is not None:
            return self.py_tracker.find_player_on_fullscreen_map(frame)

        return None

    def get_minimap_heading(self, minimap_crop: np.ndarray) -> Optional[float]:
        return self.py_tracker.get_minimap_heading(minimap_crop)

    def get_minimap_camera_heading(self, minimap_crop: np.ndarray) -> Optional[float]:
        return self.py_tracker.get_minimap_camera_heading(minimap_crop)
