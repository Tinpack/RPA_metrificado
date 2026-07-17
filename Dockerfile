FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOCKER_CONTAINER=true \
    HEADLESS=true \
    DOWNLOAD_DIR=/data/downloads \
    HISTORY_FILE=/data/historico_downloads.json \
    REPORTS_DIR=/data/reports

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

RUN mkdir -p /data/downloads /data/reports

CMD ["python", "main.py"]
