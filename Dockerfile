FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

# системный ffmpeg (с libvpx для WEBM-баннеров с прозрачностью)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# код вне /app: на Bothost /app монтируется отдельно, а /app/data — постоянный том
WORKDIR /usr/src/app
COPY requirements.txt .
RUN pip install -r requirements.txt && (pip install --only-binary=:all: cryptg || true)

COPY config.py fast_telethon.py video_processor.py bot.py ./

CMD ["python", "bot.py"]
