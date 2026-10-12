#!/usr/bin/env bash
# Запуск AdClipper на Linux/macOS без Docker
set -e
cd "$(dirname "$0")"

if [ -z "$BOT_TOKEN" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ ! -d venv ]; then
  echo "[*] Создаю виртуальное окружение..."
  python3 -m venv venv
fi
. venv/bin/activate

echo "[*] Устанавливаю зависимости..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# авто-обновление yt-dlp (YouTube часто ломает старые версии)
pip install -q -U yt-dlp || true
pip install -q --only-binary=:all: cryptg || echo "[i] cryptg недоступен — работаем без него"

echo "[*] Запускаю бота..."
while true; do
  python bot.py && break
  echo "[!] Бот упал, перезапуск через 5 сек..."
  sleep 5
done
