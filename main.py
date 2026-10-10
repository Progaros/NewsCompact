"""Entrypoint: uvicorn serving the FastAPI app (see app/api.py)."""
import logging

import uvicorn

from app import config

# App loggers (news.*) log at INFO: pipeline/harness progress, ingest stats.
logging.basicConfig(level=logging.INFO,
                      format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    uvicorn.run("app.api:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
