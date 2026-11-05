"""Optional AI scoring of passages with a small cross-encoder that runs on this machine's CPU.

Turned on by DEEPSIFT_RERANKER=<model name> (for example Xenova/ms-marco-MiniLM-L-6-v2); needs the
`fastembed` package. The model reads (question, passage) pairs and says how well each passage answers
the question, which ranks results far better than word counting alone. No outside service is called
after the model files are downloaded once.
"""
from __future__ import annotations

import math
import re
import threading
from typing import List, Optional, Sequence

_OPERATORS = re.compile(r'(?<![\w-])-?(site|inurl|intitle|filetype):\S+|\bOR\b|["()]', re.I)


def clean_question(query: str) -> str:
    """Search operators help a search engine but only confuse the scorer."""
    return " ".join(_OPERATORS.sub(" ", query).split())


def to_unit(logit: float) -> float:
    """Raw model output -> 0..1."""
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))


class ScorerBusy(RuntimeError):
    """The scorer is working on another search; the caller ranks this one by word count instead."""


class Reranker:
    def __init__(self, model: str, cache_dir: Optional[str] = None, threads: Optional[int] = None,
                 wait: Optional[float] = None) -> None:
        self.model_name = model
        self._cache_dir = cache_dir
        self._threads = threads
        self._wait = wait  # seconds to wait for a busy scorer; None = as long as it takes
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                self._model = TextCrossEncoder(self.model_name, cache_dir=self._cache_dir, threads=self._threads)
        return self._model

    def warm(self) -> None:
        self._load()

    def scores(self, question: str, passages: Sequence[str]) -> List[float]:
        """Raw relevance per passage (higher is better), same order as given."""
        if not passages:
            return []
        model = self._load()
        # One ONNX session; calls go one at a time. On a small machine several searches at once would
        # queue here for many seconds, so after `wait` seconds give up and let word counting rank it.
        if not self._lock.acquire(timeout=-1 if self._wait is None else self._wait):
            raise ScorerBusy("AI scorer busy")
        try:
            return [float(s) for s in model.rerank(question, [p[:600] for p in passages], batch_size=8)]
        finally:
            self._lock.release()
