@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

:: Check whether Docker is available in PATH.
where docker >nul 2>nul
if errorlevel 1 (
    echo Docker was not found. Please install and start Docker Desktop.
    echo.
    echo Development mode:
    echo   1. Double-click start-db.bat to start PostgreSQL only.
    echo   2. Double-click run-local-backend.bat to start the backend.
    pause
    exit /b 1
)

echo ============================================
echo  Multilingual Ancient Books Page Pool System
echo ============================================
echo.
echo Starting Docker Compose full-stack mode...
echo Open http://127.0.0.1:8001/ after startup is complete.
echo.
docker compose up --build
if errorlevel 1 (
    echo.
    echo Startup failed. Please check the error messages above.
    pause
    exit /b 1
)
endlocal
