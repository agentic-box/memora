FROM python:3.12-slim

WORKDIR /app

# README.md is required: pyproject.toml declares readme = "README.md",
# so setuptools reads it during metadata generation. PR #40 omitted it,
# which makes `pip install .` fail.
COPY pyproject.toml README.md ./
COPY memora/ memora/

RUN pip install --no-cache-dir .

# streamable-http is the documented transport (PR #40 defaulted to legacy sse).
ENV MEMORA_TRANSPORT=streamable-http \
    MEMORA_HOST=0.0.0.0 \
    MEMORA_PORT=8000 \
    MEMORA_DB_PATH=/data/memora.db \
    MEMORA_ALLOW_ANY_TAG=1

VOLUME ["/data"]
EXPOSE 8000
CMD ["memora-server"]
