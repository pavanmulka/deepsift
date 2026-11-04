FROM apify/actor-python:3.11

COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt

COPY deepsift ./deepsift
COPY actor_main.py ./

# Hosted copies never call `claude -p` (see deepsift/actor.py).
ENV DEEPSIFT_LLM=0
CMD ["python3", "actor_main.py"]
