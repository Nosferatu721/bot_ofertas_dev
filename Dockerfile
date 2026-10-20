# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# El scraper solo usa la libreria estandar: no hay dependencias que instalar.
COPY ultima_oferta.py panel.html ./

# /datos es el punto de montaje del JSON acumulado.
RUN useradd --create-home --uid 10001 scraper \
    && mkdir -p /datos \
    && chown scraper:scraper /datos

USER scraper

EXPOSE 8080

# Parada limpia: el script atiende SIGTERM y cierra el ciclo en curso.
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "ultima_oferta.py"]
CMD []
