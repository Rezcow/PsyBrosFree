# Imagen base
FROM python:3.12-slim

# Evita bytecode y buffer, fija TZ
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Santiago

# Paquetes del sistema (ffmpeg es clave para MP3)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates git tzdata \
 && rm -rf /var/lib/apt/lists/*

# Crea usuario no-root
RUN useradd -ms /bin/bash appuser

# Directorio de trabajo
WORKDIR /app

# Copia requirements primero para aprovechar cache
COPY requirements.txt /app/requirements.txt

# Actualiza pip e instala dependencias
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt

# Copia el resto del proyecto
COPY . /app

# Crea carpeta de descargas con permisos
RUN mkdir -p /app/downloads \
 && chown -R appuser:appuser /app

# Cambia a usuario no-root
USER appuser

# Comando para iniciar el bot
CMD ["python", "bot.py"]
