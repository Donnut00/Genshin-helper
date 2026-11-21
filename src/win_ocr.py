import os
import sys
import time
import logging
import subprocess
import threading
from typing import Optional
import cv2
import numpy as np

from src.paths import get_ocr_cache_dir

logger = logging.getLogger("GenshinHelper.OCR")

class WindowsOCRService:
    """
    Lightweight Persistent Windows Built-in OCR Service (<15ms per query).
    Prefers in-process WinRT OCR when optional winrt packages are installed,
    and falls back cleanly to a persistent Windows.Media.Ocr worker.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.cache_dir = get_ocr_cache_dir()
        self.temp_base = os.path.abspath(os.path.join(self.cache_dir, "ocr_query"))
        self._counter = 0
        self._is_ready = False
        self._winrt_ready = False
        self._winrt_engine = None
        self._init_worker()

    def _init_winrt(self) -> bool:
        try:
            import asyncio
            from winrt.windows.media.ocr import OcrEngine  # type: ignore
            from winrt.windows.graphics.imaging import BitmapDecoder  # type: ignore
            from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream  # type: ignore

            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return False
            self._winrt_engine = (engine, asyncio, BitmapDecoder, DataWriter, InMemoryRandomAccessStream)
            self._winrt_ready = True
            self._is_ready = True
            logger.info("Windows Built-in OCR (in-process WinRT) initialized.")
            return True
        except Exception:
            self._winrt_ready = False
            self._winrt_engine = None
            return False

    def _init_worker(self):
        if self._init_winrt():
            return
        try:
            ps_script = os.path.abspath(os.path.join(self.cache_dir, "persistent_ocr.ps1"))
            # Always rewrite script to ensure stream disposal
            os.makedirs(os.path.dirname(ps_script), exist_ok=True)
            script_content = """Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' 
}
function Await($asyncOp, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $task = $m.Invoke($null, @($asyncOp))
    $task.Wait()
    return $task.Result
}
[Windows.Media.Ocr.OcrEngine,Windows.Foundation.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Foundation.Storage,ContentType=WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
Write-Output "READY"

while ($true) {
    $line = [Console]::ReadLine()
    if (-not $line -or $line -eq "QUIT") { break }
    if (Test-Path $line) {
        try {
            $fileOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync($line)
            $file = Await $fileOp ([Windows.Storage.StorageFile])
            $streamOp = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
            $stream = Await $streamOp ([Windows.Storage.Streams.IRandomAccessStream])
            $decoderOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
            $decoder = Await $decoderOp ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmapOp = $decoder.GetSoftwareBitmapAsync()
            $bitmap = Await $bitmapOp ([Windows.Graphics.Imaging.SoftwareBitmap])
            $ocrOp = $engine.RecognizeAsync($bitmap)
            $result = Await $ocrOp ([Windows.Media.Ocr.OcrResult])
            $stream.Dispose()
            Write-Output ("RESULT:" + $result.Text.Replace("`r", " ").Replace("`n", " "))
        } catch {
            Write-Output "RESULT:"
        }
    } else {
        Write-Output "RESULT:"
    }
}
"""
            with open(ps_script, "w", encoding="utf-8") as f:
                f.write(script_content)

            self.proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            line = self.proc.stdout.readline().strip()
            if "READY" in line:
                self._is_ready = True
                logger.info("Windows Built-in OCR Service successfully initialized.")
            else:
                logger.warning(f"Windows OCR Worker initialization returned: {line}")
        except Exception as e:
            logger.warning(f"Windows OCR Worker failed to initialize: {e}")
            self._is_ready = False

    def _recognize_winrt(self, roi: np.ndarray) -> Optional[str]:
        if not self._winrt_ready or not self._winrt_engine:
            return None
        try:
            engine, asyncio, BitmapDecoder, DataWriter, InMemoryRandomAccessStream = self._winrt_engine
            ok, encoded = cv2.imencode(".png", roi)
            if not ok:
                return ""
            png_bytes = encoded.tobytes()

            async def _run() -> str:
                stream = InMemoryRandomAccessStream()
                writer = DataWriter(stream.get_output_stream_at(0))
                writer.write_bytes(png_bytes)
                await writer.store_async()
                await writer.flush_async()
                writer.detach_stream()
                stream.seek(0)
                decoder = await BitmapDecoder.create_async(stream)
                bitmap = await decoder.get_software_bitmap_async()
                res = await engine.recognize_async(bitmap)
                return str(getattr(res, "text", "") or "").replace("\r", " ").replace("\n", " ").strip()

            return asyncio.run(_run())
        except Exception as e:
            logger.debug(f"WinRT OCR error, falling back to worker: {e}")
            self._winrt_ready = False
            self._winrt_engine = None
            return None

    def recognize_text(self, roi: np.ndarray) -> str:
        """
        Recognizes text from a BGR or Grayscale image array (<15ms, thread-safe).
        Returns recognized string or empty string.
        """
        with self._lock:
            if not self._is_ready:
                self._init_worker()

            if self._winrt_ready:
                winrt_res = self._recognize_winrt(roi)
                if winrt_res is not None:
                    return winrt_res
                self._init_worker()

            if not self._is_ready or self.proc is None or self.proc.poll() is not None:
                # Attempt re-init once
                self._init_worker()
                if not self._is_ready or self.proc is None:
                    return ""

            try:
                self._counter += 1
                query_file = f"{self.temp_base}_{self._counter % 6}.png"
                cv2.imwrite(query_file, roi)
                self.proc.stdin.write(query_file + "\n")
                self.proc.stdin.flush()
                res_line = self.proc.stdout.readline().strip()
                if res_line.startswith("RESULT:"):
                    return res_line[7:].strip()
            except Exception as e:
                logger.warning(f"OCR query error: {e}")
            return ""

    def close(self):
        self._winrt_ready = False
        self._winrt_engine = None
        proc = self.proc
        self.proc = None
        self._is_ready = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    if proc.stdin:
                        proc.stdin.write("QUIT\n")
                        proc.stdin.flush()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=0.5)
                        except Exception:
                            pass
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
