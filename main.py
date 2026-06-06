"""Apify Actor entry point — Spotify→Notion playlist sync.

Reads 4 creds from environment variables.
All sync logic lives in sync.py::run_sync — nothing is duplicated here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from apify import Actor
from sol.exceptions import DedupGuardError, SolError
from sol.runs.log import log_agent_run

from sync import run_sync

ACTOR_NAME = "solos-spotify-notion-sync"

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}

        client_id = os.environ.get("solifyid")
        client_secret = os.environ.get("solifysec")
        refresh_token = os.environ.get("resolify")
        notion_token = os.environ.get("solinotion")

        missing = [
            name
            for name, val in [
                ("solifyid", client_id),
                ("solifysec", client_secret),
                ("resolify", refresh_token),
                ("solinotion", notion_token),
            ]
            if not val
        ]
        if missing:
            raise ValueError(f"Missing env vars: {', '.join(missing)}")

        dry_run: bool = bool(inp.get("dry_run", True))
        update_songs: bool = bool(inp.get("update_songs", False))

        started_at = datetime.now(timezone.utc)
        stats: dict | None = None
        error: str | None = None

        try:
            stats = run_sync(
                client_id=client_id,  # type: ignore[arg-type]
                client_secret=client_secret,  # type: ignore[arg-type]
                refresh_token=refresh_token,  # type: ignore[arg-type]
                notion_token=notion_token,  # type: ignore[arg-type]
                dry_run=dry_run,
                update_songs=update_songs,
            )
            await Actor.push_data({"stats": stats, "dry_run": dry_run})
        except (DedupGuardError, SolError, Exception) as exc:
            error = str(exc)
            log.error("Sync failed: %s", exc)
            raise
        finally:
            if notion_token:
                try:
                    log_agent_run(
                        notion_token=notion_token,
                        actor=ACTOR_NAME,
                        started_at=started_at,
                        dry_run=dry_run,
                        stats=stats,
                        error=error,
                    )
                except Exception as log_exc:
                    # Log but don't mask the original sync error
                    log.error("log_agent_run failed (non-fatal): %s", log_exc)


if __name__ == "__main__":
    asyncio.run(main())
