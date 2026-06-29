@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  Multilingual Ancient Books Page Pool System - Dev DB
echo ============================================
echo.
echo Starting PostgreSQL container only.
echo Start the backend with run-local-backend.bat.
echo.
docker compose up -d postgres
if errorlevel 1 (
    echo.
    echo Docker startup failed. Please make sure Docker Desktop is running.
    pause
    exit /b 1
)
echo.
echo PostgreSQL is running.
echo Now run run-local-backend.bat and open http://127.0.0.1:8000/
echo.
pause
