import os
import sys
import time
import logging
import subprocess
from typing import Optional
import cv2
import numpy as np

logger = logging.getLogger("GenshinHelper.OCR")

class WindowsOCRService:
    """
    Lightweight Persistent Windows Built-in OCR Service (<15ms per query).
    Uses Windows.Media.Ocr without requiring any external pip dependencies or administrator rights.
    Gracefully degrades to empty string if Windows OCR is unavailable.
    """
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.temp_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scratch", "ocr_query.png"))
        self._is_ready = False
        self._init_worker()

    def _init_worker(self):
        try:
            ps_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scratch", "persistent_ocr.ps1"))
            if not os.path.exists(ps_script):
                # Write persistent script if missing
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
        Write-Output ("RESULT:" + $result.Text.Replace("`r", " ").Replace("`n", " "))
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
                bufsize=1
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

    def recognize_text(self, roi: np.ndarray) -> str:
        """
        Recognizes text from a BGR or Grayscale image array (<15ms).
        Returns recognized string or empty string.
        """
        if not self._is_ready or self.proc is None or self.proc.poll() is not None:
            # Attempt re-init once
            self._init_worker()
            if not self._is_ready or self.proc is None:
                return ""

        try:
            cv2.imwrite(self.temp_file, roi)
            self.proc.stdin.write(self.temp_file + "\n")
            self.proc.stdin.flush()
            res_line = self.proc.stdout.readline().strip()
            if res_line.startswith("RESULT:"):
                return res_line[7:].strip()
        except Exception as e:
            logger.warning(f"OCR query error: {e}")
        return ""

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
            self._is_ready = False
