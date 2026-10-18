FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY cities ./cities
COPY prompts ./prompts

RUN pip install --no-cache-dir .

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)" || exit 1

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn relocant_media.api.app:app --host 0.0.0.0 --port 8080"]
