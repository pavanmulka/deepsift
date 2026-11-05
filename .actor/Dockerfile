FROM apify/actor-python:3.11

COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt

# The AI scorer (DEEPSIFT_RERANKER). The model is downloaded once at build time, so a run never
# fetches it. Same model and fastembed version as deploy/Dockerfile.
ARG RERANKER=Xenova/ms-marco-MiniLM-L-6-v2
# One scorer thread: a 1 GB Actor gets a quarter of a CPU core, and one thread per host core
# only fight over it. When several searches arrive at once, a search waits at most 2 s for the
# scorer and otherwise is ranked by word count, so nobody queues behind it.
ENV DEEPSIFT_RERANKER=$RERANKER \
    DEEPSIFT_RERANKER_CACHE=/models \
    DEEPSIFT_RERANKER_THREADS=1 \
    DEEPSIFT_RERANKER_WAIT=2 \
    DEEPSIFT_RERANKER_PASSAGES=1
RUN pip install --no-cache-dir fastembed==0.9.0 && \
    python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder as T; T('$RERANKER', cache_dir='/models')" && \
    chmod -R a+rX /models

COPY deepsift ./deepsift
COPY actor_main.py ./

# Hosted copies never call `claude -p` (see deepsift/actor.py).
# Google: one try, 8 s. Yahoo is asked at the same time and answers in under a second, so a
# second Google try would only make the search wait longer (and an empty page tried again is
# billed again). Tested on Apify 2026-11-05: retries did not get more Google pages back.
ENV DEEPSIFT_LLM=0 \
    DEEPSIFT_GOOGLE_TIMEOUT=8 \
    DEEPSIFT_GOOGLE_RETRIES=0
CMD ["python3", "actor_main.py"]
