@echo off
rem Baut dist\StreamApp\StreamApp.exe (Ordner komplett weitergeben).
rem Wenn ffmpeg\ffmpeg.exe neben diesem Skript liegt, wird es mit eingepackt.
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller || goto :error

.venv\Scripts\pyinstaller --noconfirm --windowed --name StreamApp main.py || goto :error

if exist ffmpeg\ffmpeg.exe (
    mkdir dist\StreamApp\ffmpeg 2>nul
    copy /y ffmpeg\ffmpeg.exe dist\StreamApp\ffmpeg\ >nul
    echo ffmpeg.exe wurde mitkopiert.
) else (
    echo HINWEIS: ffmpeg\ffmpeg.exe nicht gefunden - bitte ffmpeg.exe neben StreamApp.exe legen.
)
echo.
echo Fertig: dist\StreamApp\StreamApp.exe
goto :eof

:error
echo Build fehlgeschlagen.
exit /b 1
