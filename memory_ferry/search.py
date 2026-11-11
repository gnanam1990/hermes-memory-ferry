"""Deterministic local retrieval used by both preflight parity and provider recall."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable


_TOKEN = re.compile(r"\w{2,}", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(text)]


def score(query: str, content: str) -> float:
    q = Counter(tokens(query))
    c = Counter(tokens(content))
    if not q or not c:
        return 0.0
    overlap = sum(min(q[token], c[token]) for token in q)
    coverage = overlap / sum(q.values())
    phrase = 0.25 if query.casefold().strip() in content.casefold() else 0.0
    length_penalty = 1.0 / (1.0 + math.log1p(max(0, len(c) - 80)) / 5.0)
    return round((coverage + phrase) * length_penalty, 8)


def rank(query: str, rows: Iterable[dict], *, limit: int = 8) -> list[dict]:
    ranked = []
    for row in rows:
        value = score(query, str(row.get("content", "")))
        if value > 0:
            ranked.append({**row, "score": value})
    ranked.sort(key=lambda item: (-item["score"], str(item.get("source_identity", ""))))
    return ranked[:limit]


def jaccard(left: str, right: str) -> float:
    a, b = set(tokens(left)), set(tokens(right))
    return len(a & b) / len(a | b) if a or b else 1.0

