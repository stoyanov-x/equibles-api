FROM python:3.12-slim-bookworm

# Non-root, matching the house pattern (meridian-core uses uid 10001).
RUN useradd --create-home --uid 10001 api

WORKDIR /app

# pyproject before src so the dependency layer caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER api

ENV EQUIBLES_API_HOST=0.0.0.0 \
    EQUIBLES_API_PORT=8080

EXPOSE 8080

# /healthz is unauthenticated precisely so this works when EQUIBLES_API_KEY is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "equibles_api"]
