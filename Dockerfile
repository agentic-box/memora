FROM python:3.12-slim

WORKDIR /app

# README.md is required: pyproject.toml declares readme = "README.md",
# so setuptools reads it during metadata generation. PR #40 omitted it,
# which makes `pip install .` fail.
COPY pyproject.toml README.md ./
COPY memora/ memora/
# The operator tool, so scripts/lp_container.sh can run it in a one-off
# container of THIS image (the tool must match the image's memora), and
# scripts/cutover_store.sh with `docker exec memora-all python
# /app/scripts/local_primary.py ...` (seeding /data/<db>.db must run inside
# the container: the named volume is root-owned on the host). Code only; no
# token or data is in the image.
COPY scripts/local_primary.py scripts/local_primary.py

RUN pip install --no-cache-dir .

# streamable-http is the documented transport (PR #40 defaulted to legacy sse).
ENV MEMORA_TRANSPORT=streamable-http \
    MEMORA_HOST=0.0.0.0 \
    MEMORA_PORT=8000 \
    MEMORA_DB_PATH=/data/memora.db \
    MEMORA_ALLOW_ANY_TAG=1 \
    MEMORA_SERVICE_LOCK=1

VOLUME ["/data"]
EXPOSE 8000
CMD ["memora-server"]
