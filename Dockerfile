FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY memora/ memora/

RUN pip install --no-cache-dir .

ENV MEMORA_TRANSPORT=sse
ENV MEMORA_HOST=0.0.0.0
ENV MEMORA_PORT=8000
ENV MEMORA_STORAGE_URI=/data/memora.db

VOLUME ["/data"]

EXPOSE 8000

CMD ["memora-server"]
