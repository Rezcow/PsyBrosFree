FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN echo "==== requirements.txt ====" && cat requirements.txt && \
    pip install --no-cache-dir "python-telegram-bot[webhooks]==21.4" && \
    pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]

