@echo off
rem Double-click to install Job Assistant (Docker Desktop). Add -Native to install without Docker.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
