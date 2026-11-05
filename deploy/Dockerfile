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
