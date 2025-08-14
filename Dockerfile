FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Debug opcional: imprime el requirements en el build log
RUN echo "==== requirements.txt ====" && cat requirements.txt

# Fuerza PTB con webhooks y luego instala el resto
RUN pip install --no-cache-dir "python-telegram-bot[webhooks]==21.4" \
 && pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
