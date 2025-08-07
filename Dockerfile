# Imagen base con Python
FROM python:3.12-slim

# Instala ffmpeg y utilidades básicas
RUN apt-get update && apt-get install -y ffmpeg git && apt-get clean

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia todos los archivos del proyecto
COPY . /app

# Instala las dependencias desde requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Comando para iniciar el bot
CMD ["python", "bot.py"]
