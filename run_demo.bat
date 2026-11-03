@echo off
cd /d "%~dp0"
start "relay" cmd /k python relay.py
timeout /t 1 /nobreak >nul
start "gateway" cmd /k python gateway.py
timeout /t 1 /nobreak >nul
start "parent" cmd /k python parent.py
timeout /t 1 /nobreak >nul
start "" http://127.0.0.1:8080
