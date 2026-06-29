@echo off
chcp 65001 >nul
cd /d "%~dp0\backend"

if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
) else (
  echo Virtual environment not found at ..\.venv
  echo Please create a venv first: python -m venv ..\.venv
  pause
  exit /b 1
)
