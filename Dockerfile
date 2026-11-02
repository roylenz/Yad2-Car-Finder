FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_PATH=/usr/bin/chromium \
    CHROME_HEADLESS=false \
    CHROME_NO_SANDBOX=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN mkdir -p /app/logs /app/data && chown -R bot:bot /app

USER bot
CMD ["xvfb-run", "-a", "python", "src/main.py"]
