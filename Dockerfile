FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/root/.local/bin:${PATH}" \
    STRAZH_MAIL_URL=https://127.0.0.1:8090 \
    STRAZH_MODEL=qwen2.5:3b \
    STRAZH_NUM_CTX=4096

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssl \
        build-essential \
        libsqlcipher-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt 'uv==0.13.0' \
    && uv tool install 'atomno-mcp-cbr-rates==0.1.11'

COPY src /app/src
COPY static /app/static
COPY examples /app/examples
COPY langgraph.json /app/langgraph.json
COPY entrypoint.py /app/entrypoint.py

RUN mkdir -p /app/data \
    && chmod 755 /app/entrypoint.py

EXPOSE 8080 8090

CMD ["python", "/app/entrypoint.py"]
