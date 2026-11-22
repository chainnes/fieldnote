from __future__ import annotations

import uvicorn

from fieldnote.app import create_app
from fieldnote.config import Settings


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
