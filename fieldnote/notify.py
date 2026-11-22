"""Optional n8n webhook when a draft is ready.

If N8N_WEBHOOK_URL is empty, this is a no-op. A failed post does not fail the draft.
"""

from __future__ import annotations

import json
import urllib.request


def notify_draft_ready(url: str, payload: dict) -> str:
    if not (url or "").strip():
        return "skipped"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url.strip(),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return f"sent {response.status}"
    except Exception as exc:
        return f"failed: {exc}"
