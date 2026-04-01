#!/bin/bash
set -e

echo "🚀 Установка Content Bot..."

cd "$(dirname "$0")"

# Python venv
python3 -m venv venv
source venv/bin/activate

# Зависимости
pip install --upgrade pip
pip install -r requirements.txt

# yt-dlp (свежая версия через pip)
pip install -U yt-dlp

# Папка для медиафайлов
mkdir -p media

echo ""
echo "✅ Готово! Команды:"
echo "  source venv/bin/activate"
echo "  python main.py fetch    # собрать контент"
echo "  python main.py status   # статус очереди"
echo "  python main.py post     # тестовая публикация"
echo "  python main.py run      # запустить планировщик"
