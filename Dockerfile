FROM python:3.11-slim

# Dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Reqs primero para aprovechar cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY bot.py .

ENV PYTHONUNBUFFERED=1
# Opcional: logs de yt-dlp un poco más silenciosos
ENV YTDLP_NO_UPDATE=1

CMD ["python", "bot.py"]
