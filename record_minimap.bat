@echo off
:: Developer benchmark: launches Genshin Helper and losslessly records 90 s of in-game minimap frames
:: to scratch\minimap_rec\rec_YYYYMMDD_HHMMSS\ (only while the game is focused and the minimap is visible).
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)
title "Genshin Helper - Minimap Recording [ADMIN]"
cd /d "%~dp0"
echo ====================================================================
echo   GHI MINIMAP 90 GIAY (benchmark huong camera)
echo   1. Vao game, de minimap hien thi. Thoi gian chi chay khi game o phia truoc.
echo   2. Xoay camera cham/nhanh, dung yen, chay; o co, tuyet, nuoc, thanh pho, hang.
echo   3. Khi xong, Mini-HUD hien "REC OK" va log ghi duong dan thu muc.
echo ====================================================================
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
.\.venv\Scripts\python.exe main.py --record-minimap 90
pause
