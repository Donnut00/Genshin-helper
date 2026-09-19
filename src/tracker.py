import os
import sys
import ctypes
import logging
from typing import Optional, Tuple
import cv2
import numpy as np

logger = logging.getLogger("GenshinHelper.Tracker")

class CvAutoTrackBridge:
    """
    Direct ctypes bridge to cvAutoTrack.dll (the standard C++ engine used by Kongying Tavern).
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
            # Define function signatures
            # bool init()
            self._lib.init.restype = ctypes.c_bool
            # bool uninit()
            self._lib.uninit.restype = ctypes.c_bool
            # bool GetTransform(double &x, double &y, double &a)
            self._lib.GetTransform.argtypes = [
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double)
            ]
            self._lib.GetTransform.restype = ctypes.c_bool

            if self._lib.init():
                self.is_loaded = True
                logger.info(f"Loaded cvAutoTrack.dll successfully from {self.dll_path}")
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
    Pure Python/OpenCV Tracker that identifies player position and facing angle.
    Supports:
    1. Minimap player arrow tracking (HSV cyan arrow detection + heading angle).
    2. Fullscreen Map (M) player marker tracking.
    """
    def __init__(self):
        # HSV bounds for the player's cyan arrow icon in Genshin Impact
        # Hue: 85 - 105, Saturation: 160 - 255, Value: 180 - 255
        self.cyan_lower = np.array([85, 160, 180], dtype=np.uint8)
        self.cyan_upper = np.array([105, 255, 255], dtype=np.uint8)

        # Yellow ping circle HSV bounds
        self.yellow_lower = np.array([20, 120, 180], dtype=np.uint8)
        self.yellow_upper = np.array([35, 255, 255], dtype=np.uint8)

    def find_player_on_fullscreen_map(self, map_frame: np.ndarray) -> Optional[Tuple[float, float, float]]:
        """
        Locates the cyan player arrow on the opened full map (M).
        Returns (pixel_x, pixel_y, angle_degrees) relative to the frame.
        """
        if map_frame is None or map_frame.size == 0:
            return None

        hsv = cv2.cvtColor(map_frame, cv2.COLOR_BGR2HSV)
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

        # Calculate arrow rotation angle using minimum area bounding box or fitEllipse
        angle = 0.0
        if len(largest) >= 5:
            ellipse = cv2.fitEllipse(largest)
            angle = ellipse[2]

        return (cx, cy, angle)

    def get_minimap_heading(self, minimap_crop: np.ndarray) -> float:
        """
        Finds the orientation of the arrow at the center of the minimap.
        """
        if minimap_crop is None or minimap_crop.size == 0:
            return 0.0

        h, w = minimap_crop.shape[:2]
        center_crop = minimap_crop[int(h*0.35):int(h*0.65), int(w*0.35):int(w*0.65)]
        if center_crop.size == 0:
            return 0.0

        hsv = cv2.cvtColor(center_crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.cyan_lower, self.cyan_upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return 0.0

        largest = max(contours, key=cv2.contourArea)
        if len(largest) >= 5:
            ellipse = cv2.fitEllipse(largest)
            return float(ellipse[2])
        return 0.0


class HybridTracker:
    """Unified tracker combining cvAutoTrack (if available) with pure OpenCV fallback."""
    def __init__(self):
        self.c_tracker = CvAutoTrackBridge()
        self.py_tracker = PureOpenCVTracker()

    def track(self, frame: Optional[np.ndarray]) -> Optional[Tuple[float, float, float]]:
        # 1. First priority: cvAutoTrack C++ engine (highest precision across all Teyvat)
        if self.c_tracker.is_loaded:
            t = self.c_tracker.get_transform()
            if t is not None:
                return t

        # 2. Fallback: Pure OpenCV visual detection on frame
        if frame is not None:
            return self.py_tracker.find_player_on_fullscreen_map(frame)

        return None
