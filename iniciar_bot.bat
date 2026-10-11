@echo off
title Bot Telegram - MT5
set "BOT=%USERPROFILE%\Desktop\tg_mt5_bot"
cd /d "%BOT%"
if not exist "main.py" (
    echo No encuentro main.py en "%BOT%".
    pause
    exit /b
)

set "MT5=C:\Program Files\MetaTrader 5\terminal64.exe"

tasklist /fi "imagename eq terminal64.exe" | find /i "terminal64.exe" >nul
if errorlevel 1 (
    echo Abriendo MetaTrader 5...
    start "" /min "%MT5%"
    echo Esperando 45 segundos a que MT5 inicie sesion...
    timeout /t 45 /nobreak >nul
)

call venv\Scripts\activate.bat

:bucle
echo.
echo ===== Bot en marcha. Minimiza esta ventana, NO la cierres =====
python main.py
echo.
echo El bot se ha detenido. Se reinicia solo en 30 segundos...
echo (Para pararlo del todo, cierra esta ventana)
timeout /t 30 >nul
goto bucle