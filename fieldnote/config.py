from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    # Already-set environment variables win over the file.
    load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    api_key: str
    base_url: str
    temperature: float
    db_path: Path
    seed_dir: Path
    as_of: date
    host: str
    port: int
    change_shift_days: int = 3
    n8n_webhook_url: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        provider = os.environ.get("LLM_PROVIDER", "mock").strip().lower() or "mock"
        if provider == "openai_compatible":
            provider = "compatible"
        seed = Path(os.environ.get("FIELDNOTE_SEED") or (PACKAGE / "seed"))
        db = Path(os.environ.get("FIELDNOTE_DB") or (ROOT / "data" / "fieldnote.db"))
        as_of_raw = os.environ.get("AS_OF_DATE", "2026-03-25").strip()
        try:
            as_of = datetime.strptime(as_of_raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise SystemExit(f"AS_OF_DATE must be YYYY-MM-DD, got {as_of_raw!r}") from exc
        port_raw = os.environ.get("PORT") or os.environ.get("FIELDNOTE_PORT") or "43123"
        try:
            temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        except ValueError:
            temperature = 0.0
        try:
            shift_days = int(os.environ.get("CHANGE_SHIFT_DAYS", "3"))
        except ValueError:
            shift_days = 3
        return cls(
            provider=provider,
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini").strip(),
            api_key=os.environ.get("LLM_API_KEY", "").strip(),
            base_url=os.environ.get("LLM_BASE_URL", "").strip(),
            temperature=temperature,
            db_path=db,
            seed_dir=seed,
            as_of=as_of,
            host=os.environ.get("FIELDNOTE_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=int(port_raw),
            change_shift_days=shift_days,
            n8n_webhook_url=os.environ.get("N8N_WEBHOOK_URL", "").strip(),
        )

    @property
    def provider_label(self) -> str:
        if self.provider == "mock":
            return "mock rules"
        if self.provider == "compatible":
            return f"compatible · {self.model}"
        return f"{self.provider} · {self.model}"
