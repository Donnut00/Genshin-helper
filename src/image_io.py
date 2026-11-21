# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/image_io.py (MIT License)
# Copyright (c) 2026 MaxWay12
# `write_json_atomic` and retryable replace helpers are clean-room original implementations.
"""Unicode-safe OpenCV image I/O and atomic JSON writer with Windows lock retry."""
from __future__ import annotations

import errno
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

import cv2
import numpy as np

PathLike = Union[str, os.PathLike[str], Path]

_RETRYABLE_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EBUSY})
# Windows error codes: ACCESS_DENIED (5), SHARING_VIOLATION (32), LOCK_VIOLATION (33)
_RETRYABLE_WINERRORS = frozenset({5, 32, 33})


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or getattr(exc, "errno", None) in _RETRYABLE_ERRNOS
        or getattr(exc, "winerror", None) in _RETRYABLE_WINERRORS
    )


def _replace_with_retry(
    src: PathLike,
    dst: PathLike,
    retries: int = 5,
    retry_delay_s: float = 0.05,
) -> None:
    max_attempts = max(0, int(retries)) + 1
    delay = max(0.0, float(retry_delay_s))
    last_exc: Optional[OSError] = None
    for attempt in range(max_attempts):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts or not _is_retryable_replace_error(exc):
                raise
            if delay > 0.0:
                time.sleep(delay)
    if last_exc is not None:
        raise last_exc


def read_image(path: PathLike, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """Unicode-safe replacement for cv2.imread. Returns None for missing, empty, or corrupt images."""
    try:
        data = Path(path).read_bytes()
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags) if data else None
    except (OSError, cv2.error):
        return None


def write_image(
    path: PathLike,
    img: np.ndarray,
    ext: Optional[str] = None,
) -> bool:
    """Unicode-safe, atomic replacement for cv2.imwrite using NamedTemporaryFile + os.replace."""
    destination = Path(path)
    temporary: Optional[str] = None
    replaced = False
    try:
        suffix = ext if ext else destination.suffix
        if not suffix:
            suffix = ".png"
        if not suffix.startswith("."):
            suffix = "." + suffix

        ok, encoded = cv2.imencode(suffix, img)
        if not ok:
            return False

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".image-",
            suffix=".tmp",
            dir=str(destination.parent),
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(encoded.tobytes())
            stream.flush()
            os.fsync(stream.fileno())

        _replace_with_retry(temporary, destination)
        replaced = True
        return True
    except (OSError, cv2.error):
        return False
    finally:
        if temporary is not None and not replaced:
            Path(temporary).unlink(missing_ok=True)


def write_json_atomic(
    path: PathLike,
    data: Any,
    retries: int = 5,
    retry_delay_s: float = 0.05,
    *,
    indent: int = 4,
    ensure_ascii: bool = False,
) -> bool:
    """
    Atomically writes UTF-8 JSON via temp file + flush + os.fsync + os.replace,
    retrying transient Windows Defender / OneDrive lock errors (EPERM/EACCES/EBUSY).
    """
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary: Optional[str] = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".json-",
            suffix=".tmp",
            dir=str(destination.parent),
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        _replace_with_retry(temporary, destination, retries=retries, retry_delay_s=retry_delay_s)
        replaced = True
        return True
    finally:
        if temporary is not None and not replaced:
            Path(temporary).unlink(missing_ok=True)
