# Build from the parent directory so the sibling velocidrone libraries are reachable:
#   docker build -f splitter/Dockerfile ..   (compose does this for you)
FROM python:3.12-slim

WORKDIR /app

COPY velocidrone-libraries/velocidrone-websocket /app/velocidrone-websocket
COPY velocidrone-libraries/velocidrone-api /app/velocidrone-api
COPY splitter /app/splitter

RUN pip install --no-cache-dir /app/velocidrone-websocket /app/velocidrone-api /app/splitter

VOLUME /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/splitter.db
EXPOSE 8100

CMD ["python", "-m", "splitter"]
