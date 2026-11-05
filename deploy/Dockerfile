# Self-hosted DeepSift API server for your own apps (for example on a VPS, next to the app that uses it).
# Google results come through Apify's GOOGLE_SERP proxy, billed to your Apify account per results page.
#
#   docker build -f deploy/Dockerfile -t deepsift .
#   docker run -e DEEPSIFT_API_KEY=... -e DEEPSIFT_SERP_PROXY_URL=http://groups-GOOGLE_SERP:<proxy password>@proxy.apify.com:8000 deepsift
#
# It speaks Tavily's /search, /extract, /crawl and /map on port 8787. It never calls `claude -p`.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN grep -v '^pytest' requirements.txt > /tmp/req.txt && pip install --no-cache-dir -r /tmp/req.txt lxml

# The AI scorer (DEEPSIFT_RERANKER). The model is downloaded once at build time, so the server never
# fetches it at run time. Build with --build-arg RERANKER= to leave it out.
ARG RERANKER=Xenova/ms-marco-MiniLM-L-6-v2
ENV DEEPSIFT_RERANKER_CACHE=/models
RUN if [ -n "$RERANKER" ]; then \
      pip install --no-cache-dir fastembed==0.9.0 && \
      python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder as T; T('$RERANKER', cache_dir='/models')" && \
      chmod -R a+rX /models; \
    fi

COPY deepsift ./deepsift

ENV DEEPSIFT_LLM=0 \
    DEEPSIFT_BACKENDS=google \
    DEEPSIFT_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN useradd --create-home deepsift && mkdir -p /data && chown deepsift /data
USER deepsift
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import socket;socket.create_connection(('127.0.0.1',8787),3)" || exit 1
CMD ["python", "-W", "ignore", "-m", "uvicorn", "deepsift.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
