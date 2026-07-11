@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Create it and install the project before using this launcher.
  echo See README.md for setup instructions.
  exit /b 1
)
echo Starting tuner on http://localhost:8099 ...
echo Press Ctrl+C in this window to stop cleanly.
".venv\Scripts\python.exe" -m tuner_app
