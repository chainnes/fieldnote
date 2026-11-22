"""Deterministic local embeddings.

Mock mode and the tests never call an embedding API. The same function is the
fallback when no pgvector database is configured. It is a hashed bag of words,
not a semantic model: shared words raise the score, and the vector for a string
does not change between runs.
"""

from __future__ import annotations

import hashlib
import math

from fieldnote.match import normalize

DIM = 64


def embed(text: str) -> list[float]:
    vector = [0.0] * DIM
    tokens = normalize(text).split()
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[digest[0] % DIM] += 1.0 if digest[1] % 2 == 0 else -1.0
    for left, right in zip(tokens, tokens[1:]):
        digest = hashlib.sha256(f"{left} {right}".encode("utf-8")).digest()
        vector[digest[0] % DIM] += 0.5 if digest[1] % 2 == 0 else -0.5
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return round(sum(a * b for a, b in zip(left, right)), 4)


def chunk_text(text: str, size: int = 480) -> list[str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ["(empty)"]
    chunks: list[str] = []
    buffer = ""
    for line in lines:
        if buffer and len(buffer) + len(line) + 1 > size:
            chunks.append(buffer)
            buffer = line
        else:
            buffer = f"{buffer}\n{line}".strip()
    if buffer:
        chunks.append(buffer)
    return chunks
