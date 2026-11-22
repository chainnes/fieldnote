"""One repair attempt, then the review queue.

Off-schema model output is asked to retry once. If that still fails, the caller
puts a row on the review queue instead of dropping it silently or guessing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_with_repair(parse: Callable[[], T], repair: Callable[[], T]) -> tuple[T | None, str, str]:
    """Return (value, status, error).

    status is "ok", "repaired", or "review_queue".
    """
    try:
        return parse(), "ok", ""
    except Exception as first:
        try:
            return repair(), "repaired", str(first)
        except Exception as second:
            return None, "review_queue", str(second)
