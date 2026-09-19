@echo off
:: Self-elevation to Administrator (Required for global hotkey & mouse hook)
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [Genshin Helper] Dang yeu cau quyen Administrator de bat phim tat toan cuc va hook chuot...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

title "Genshin Impact Exploration - HoYoLAB Sync Helper [ADMIN]"
cd /d "%~dp0"
echo ====================================================================
echo   GENSHIN HELPER v3.0 (Da khoi chay voi quyen Administrator)
echo   Local Server: http://127.0.0.1:8765
echo   Unified Hotkey: Ho tro Ban phim + Tat ca nut Chuot (Mouse 4/5/Middle)
echo   Chinh sua phim tat truc tiep tren Web Map hoac file config.json
echo ====================================================================
echo.
:: Giai phong port 8765 neu co tien trinh cu chay ngam
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
.\.venv\Scripts\python.exe main.py
pause
