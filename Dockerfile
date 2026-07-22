FROM python:3.12-slim

# Deploy defaults — overridable via Railway env vars.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EMBEDDER_PROVIDER=fastembed \
    VECTOR_STORE=pgvector \
    MCP_TRANSPORT=streamable-http

WORKDIR /app

# mem0ai from PyPI (pinned to match the local clone), then this package + its
# lean deps (no torch/chromadb — those are the [local] extra).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install "mem0ai==2.0.12" .

# Bake the embedding model into the image so cold starts don't download it.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Railway injects $PORT at runtime; the app reads it (falls back to 8000 locally).
EXPOSE 8000
CMD ["python", "-m", "context_layer"]
