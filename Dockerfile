FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    HOME=/app/home

WORKDIR /app

RUN mkdir -p /app/home /app/data \
    && pip install --no-cache-dir --break-system-packages "camoufox[geoip]==0.5.8" \
    && yes | camoufox fetch

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --break-system-packages .

RUN chown -R 1000:1000 /app

USER 1000:1000
RUN fc-cache -f

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="avito-parser" \
      org.opencontainers.image.description="Hourly Avito search watcher that mails new listings" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.revision="$VCS_REF"

CMD ["python", "-m", "avito"]
