FROM python:3.11-slim

WORKDIR /app
<<<<<<< HEAD
=======

# Dependencias base (certificados)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

>>>>>>> 965000f (setup: archivos del bot)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
<<<<<<< HEAD

ENV PYTHONUNBUFFERED=1
=======
ENV PYTHONUNBUFFERED=1

# Render (Web Service) leerá PORT; run_webhook usará ese puerto
>>>>>>> 965000f (setup: archivos del bot)
CMD ["python", "bot.py"]
