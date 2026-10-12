@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title AdClipper Bot

rem ---- .env ----
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
)

rem ---- ищем НАСТОЯЩИЙ Python (заглушка Microsoft Store не подходит) ----
set "PY="
rem предпочитаем стабильные 3.12 / 3.13 — для них есть готовые сборки всех библиотек
for %%V in (3.12 3.13 3.11) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo [!] Python не установлен. Сейчас Windows подставляет заглушку из Microsoft Store.
  echo.
  echo     1. Скачай Python 3.12: https://www.python.org/downloads/windows/
  echo     2. При установке ОБЯЗАТЕЛЬНО отметь галочку "Add python.exe to PATH"
  echo     3. Закрой это окно и запусти start_bot.bat заново
  echo.
  echo     Если Python уже стоит: Параметры - Приложения - Дополнительные параметры -
  echo     Псевдонимы выполнения приложений - выключи python.exe и python3.exe
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo [*] Найден %%v
%PY% -c "import sys; sys.exit(0 if sys.version_info < (3,14) else 1)"
if errorlevel 1 (
  echo [!] ВНИМАНИЕ: слишком новая версия Python. Для неё часть библиотек не собрана.
  echo     Рекомендуется Python 3.12: https://www.python.org/downloads/release/python-3128/
  echo     Можно поставить рядом с текущей - скрипт сам выберет 3.12.
  echo.
)

rem ---- виртуальное окружение ----
if not exist "venv\Scripts\python.exe" (
  echo [*] Создаю виртуальное окружение...
  if exist venv rmdir /s /q venv
  %PY% -m venv venv
  if errorlevel 1 (
    echo [!] Не удалось создать venv.
    pause
    exit /b 1
  )
)
set "VPY=venv\Scripts\python.exe"

echo [*] Устанавливаю зависимости...
"%VPY%" -m pip install -q --upgrade pip
"%VPY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [!] Ошибка установки зависимостей. Проверь интернет и попробуй снова.
  pause
  exit /b 1
)
"%VPY%" -m pip install -q -U yt-dlp

rem cryptg — необязательное ускорение шифрования, только готовая сборка (без Rust)
"%VPY%" -m pip install -q --only-binary=:all: cryptg >nul 2>nul
if errorlevel 1 (
  echo [i] cryptg недоступен для этой версии Python - бот работает и без него, чуть медленнее.
)

if "%BOT_TOKEN%"=="" (
  findstr /c:"ВСТАВЬ_СЮДА_ТОКЕН_БОТА" config.py >nul && (
    echo [!] Не задан BOT_TOKEN. Создай файл .env со строкой:
    echo     BOT_TOKEN=123456:ABC...
    pause
    exit /b 1
  )
)

echo [*] FFmpeg: bin\ffmpeg.exe, иначе встроенный imageio-ffmpeg.

:loop
echo [*] Запускаю бота...
"%VPY%" bot.py
if errorlevel 1 (
  echo [!] Бот упал, перезапуск через 5 сек... ^(Ctrl+C - выход^)
  timeout /t 5 /nobreak >nul
  goto loop
)
pause
