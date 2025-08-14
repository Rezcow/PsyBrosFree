FROM python:3.11-slim

# Solo certificados (no necesitamos servidor HTTP ni ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
