@echo off
title Genshin Helper - Tampermonkey Updater
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [Loi] Khong tim thay Python! Vui long cai dat Python vao he thong.
    pause
    exit /b 1
)

python scripts\update_tampermonkey.py
if %errorlevel% neq 0 (
    pause
)
