@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose down
echo.
echo All containers have been stopped.
echo Database data is retained and will be reused on next startup.
echo.
echo To fully clear the database, run:
echo   docker compose down -v
echo.
pause
