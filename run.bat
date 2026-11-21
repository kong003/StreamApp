@echo off
rem Startet StreamApp direkt aus dem Quellcode (Entwicklung / Test).
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python main.py
