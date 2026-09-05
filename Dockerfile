FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    HOME=/app/home

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /app/home \
    && pip install --no-cache-dir --break-system-packages "camoufox[geoip]==0.5.5" \
    && yes | camoufox fetch official/152.0.4-beta.29 \
    && camoufox set official/152.0.4-beta.29

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --break-system-packages .

COPY scripts/init-data.sh /usr/local/bin/avito-init-data
RUN chmod 0755 /usr/local/bin/avito-init-data

RUN mkdir -p /app/data /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R 1000:1000 /app

USER 1000:1000
RUN fc-cache -f

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="avito-parser" \
      org.opencontainers.image.description="Avito search watcher and notifier" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.revision="$VCS_REF"

HEALTHCHECK --interval=5m --timeout=30s --start-period=1m --retries=3 \
    CMD ["python", "-m", "avito", "health"]

CMD ["sh", "-c", "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99; \
     Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp & \
     export DISPLAY=:99; \
     exec python -m avito run"]
