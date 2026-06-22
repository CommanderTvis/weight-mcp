"""Run the server with uvicorn."""

from __future__ import annotations

import uvicorn

from .config import load_settings
from .server import create_app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
