"""One-time bootstrap: create the Pinecone serverless index if it doesn't exist.

Run before the first deploy:
    python -m scripts.bootstrap_pinecone --cloud aws --region us-east-1

Picks up settings from the same .env / environment variables as the app.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import get_settings
from app.logging_config import configure_logging, get_logger
from app.pinecone_store import ensure_index


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cloud", default="aws", choices=["aws", "gcp", "azure"])
    p.add_argument("--region", default="us-east-1")
    args = p.parse_args()

    configure_logging("INFO")
    log = get_logger("bootstrap")
    settings = get_settings()

    desc = ensure_index(settings, cloud=args.cloud, region=args.region)
    log.info("bootstrap.done")
    print(json.dumps(desc, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
