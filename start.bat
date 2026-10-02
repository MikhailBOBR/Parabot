@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Установите Python 3.11 или новее с python.org. Включите Add Python to PATH.
    pause
    exit /b 1
  )
  set "PARABOT_PY=python"
) else (
  set "PARABOT_PY=py -3"
)
if not exist .venv\Scripts\python.exe (
  %PARABOT_PY% -m venv .venv
  if errorlevel 1 goto failed
)
if not exist .env (
  copy .env.example .env >nul
  echo Вставьте BOT_TOKEN в .env. Если не знаете свой ID, оставьте ADMIN_IDS пустым.
  echo После запуска отправьте боту /id, вставьте ID в ADMIN_IDS и перезапустите.
  notepad .env
  echo Сохраните файл в Блокноте и закройте Блокнот, затем нажмите любую клавишу.
  pause >nul
)
.venv\Scripts\python.exe -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto failed
echo Запускаю ПараБота. Остановка: Ctrl+C.
:run
.venv\Scripts\python.exe -m parabot
set "PARABOT_EXIT=%ERRORLEVEL%"
if "%PARABOT_EXIT%"=="0" exit /b 0
if "%PARABOT_EXIT%"=="2" goto stopped
echo Bot exited unexpectedly. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto run
:stopped
echo Check .env and the bot token. Automatic restart is disabled for configuration errors.
pause
exit /b 2
:failed
echo Не удалось подготовить запуск. Проверьте Python и доступ к интернету.
pause
exit /b 1
