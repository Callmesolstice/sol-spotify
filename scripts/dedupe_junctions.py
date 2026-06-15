"""One-off cleanup: de-duplicate the 🔗 Playlist Tracks DB.

Background: sol-enrich Phase A renamed every junction's Name, which broke
sol-spotify sync's old Name-based dedup, so a sync run re-created the full set of
junction rows (~3845 dupes). sync.py is now fixed to dedup by relation; this script
removes the dupes that already landed.

Dedup rule: group by (Song page-id, Playlist page-id). Keep the OLDEST row in each
group; archive the rest. SAFETY: a row carrying manual data (Is Seeded = true, or a
non-empty Content Pieces relation) is NEVER archived — if such a row would be
archived, it is kept instead and logged for manual review.

Archiving sets `archived: true` (moves to Notion trash — recoverable ~30 days).

Run:
  python scripts/dedupe_junctions.py            # dry run — reports only
  python scripts/dedupe_junctions.py --commit    # actually archive dupes

Reads the Notion token from the `solinotion` env var (script layer only).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

from sol.config.spotify import PLAYLIST_TRACKS_DB_ID
from sol.http import get_session
from sol.notion.core import NOTION_BASE, _headers

WRITE_SLEEP = 0.34


def _query_all(notion_token: str, database_id: str) -> list[dict]:
    session = get_session()
    headers = _headers(notion_token)
    endpoint = f"{NOTION_BASE}/databases/{database_id}/query"
    pages: list[dict] = []
    payload: dict = {}
    while True:
        resp = session.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
        pages.extend(body.get("results") or [])
        if not body.get("has_more"):
            break
        payload = {"start_cursor": body["next_cursor"]}
    return pages


def _rel_id(page: dict, prop: str) -> str | None:
    rel = page.get("properties", {}).get(prop, {}).get("relation", [])
    return rel[0]["id"] if rel else None


def _has_manual_data(page: dict) -> bool:
    props = page.get("properties", {})
    is_seeded = bool(props.get("Is Seeded", {}).get("checkbox"))
    content_pieces = props.get("Content Pieces", {}).get("relation") or []
    return is_seeded or bool(content_pieces)


def _archive(notion_token: str, page_id: str) -> None:
    session = get_session()
    resp = session.patch(
        f"{NOTION_BASE}/pages/{page_id}", headers=_headers(notion_token), json={"archived": True}
    )
    resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="actually archive dupes")
    args = parser.parse_args()

    notion_token = os.environ.get("solinotion")
    if not notion_token:
        sys.exit("Missing env var: solinotion")

    pages = _query_all(notion_token, PLAYLIST_TRACKS_DB_ID)
    print(f"Total Playlist Tracks rows: {len(pages)}")

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    orphans = 0
    for p in pages:
        song = _rel_id(p, "Song")
        playlist = _rel_id(p, "Playlist")
        if not song or not playlist:
            orphans += 1
            continue
        groups[(song, playlist)].append(p)

    to_archive: list[str] = []
    protected = 0
    dup_groups = 0
    for (_song, _pl), rows in groups.items():
        if len(rows) < 2:
            continue
        dup_groups += 1
        rows.sort(key=lambda r: r.get("created_time", ""))  # oldest first
        keeper = rows[0]
        for r in rows[1:]:
            if _has_manual_data(r):
                protected += 1  # never archive a row with manual data
                continue
            to_archive.append(r["id"])
        # If the oldest keeper somehow lacks manual data but a dup has it, the dup is
        # protected above and simply not archived — both survive, logged via `protected`.
        _ = keeper

    print(f"Unique (Song,Playlist) pairs: {len(groups)}")
    print(f"Duplicate groups (>1 row):    {dup_groups}")
    print(f"Rows to archive:              {len(to_archive)}")
    print(f"Protected (manual data) kept: {protected}")
    print(f"Orphan rows (no Song/Playlist relation, skipped): {orphans}")

    if not args.commit:
        print("\nDRY RUN — nothing archived. Re-run with --commit to archive the dupes.")
        return

    print(f"\nArchiving {len(to_archive)} duplicate rows...")
    done = 0
    for pid in to_archive:
        _archive(notion_token, pid)
        time.sleep(WRITE_SLEEP)
        done += 1
        if done % 200 == 0:
            print(f"  archived {done}/{len(to_archive)}")
    print(f"Done. Archived {done} rows (recoverable from Notion trash ~30 days).")


if __name__ == "__main__":
    main()
