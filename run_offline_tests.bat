@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0tests\run_offline.py"
