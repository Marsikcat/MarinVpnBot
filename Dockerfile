FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY scripts ./scripts

RUN useradd -r -u 1000 -d /app vpnbot     && mkdir -p /app/data     && chown -R vpnbot:vpnbot /app
VOLUME ["/app/data"]
USER vpnbot

CMD ["python", "-m", "bot.main"]
