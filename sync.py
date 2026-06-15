"""Local entry point: reads shell env, injects creds into sol module.

Usage:
    python scripts/sync.py                        # dry run
    python scripts/sync.py --commit               # seed — creates new rows, skips existing-song updates
    python scripts/sync.py --commit --update-songs  # also re-patch existing songs (popularity etc.)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sol.auth.spotify import refresh_spotify_token
from sol.config.spotify import (
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_REFRESH_TOKEN,
    PLAYLIST_TRACKS_DB_ID,
    PLAYLISTS_DB_ID,
    SONGS_DB_ID,
)
from sol.exceptions import DedupGuardError, SolError
from sol.http import get_session
from sol.notion.core import date, number, relation, rich_text, title
from sol.platforms.spotify.client import (
    get_current_user_playlists,
    get_playlist_items,
)

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
ENV_NOTION = "solinotion"
WORKERS = 5  # concurrent Notion write threads

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _query_all_pages(notion_token: str, database_id: str) -> list[dict]:
    """Full DB scan with no filter. Uses SDK session for retries + backoff."""
    session = get_session()
    headers = _notion_headers(notion_token)
    url = f"{NOTION_BASE}/databases/{database_id}/query"
    pages: list[dict] = []
    payload: dict = {}

    while True:
        resp = session.post(url, headers=headers, json=payload, timeout=60)
        if not resp.ok:
            raise SolError(f"Notion scan failed {resp.status_code}: {resp.text}")
        body = resp.json()
        pages.extend(body.get("results") or [])
        if not body.get("has_more"):
            break
        payload = {"start_cursor": body["next_cursor"]}

    return pages


def _write_page(
    notion_token: str,
    database_id: str,
    properties: dict,
    page_id: str | None = None,
    cover_url: str | None = None,
) -> str:
    """PATCH existing page or POST new one. Returns page id."""
    session = get_session()
    headers = _notion_headers(notion_token)
    extras: dict = {}
    if cover_url:
        extras["cover"] = {"type": "external", "external": {"url": cover_url}}

    if page_id:
        payload = {"properties": properties, **extras}
        resp = session.patch(
            f"{NOTION_BASE}/pages/{page_id}", headers=headers, json=payload, timeout=60
        )
    else:
        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
            **extras,
        }
        resp = session.post(
            f"{NOTION_BASE}/pages", headers=headers, json=payload, timeout=60
        )

    if not resp.ok:
        raise SolError(f"Notion write failed {resp.status_code}: {resp.text}")
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Pre-fetch indexes
# ---------------------------------------------------------------------------

def _prop_rich_text(page: dict, prop: str) -> str | None:
    vals = page.get("properties", {}).get(prop, {}).get("rich_text", [])
    return vals[0]["text"]["content"] if vals else None


def _prop_title(page: dict, prop: str) -> str | None:
    vals = page.get("properties", {}).get(prop, {}).get("title", [])
    return vals[0]["text"]["content"] if vals else None


def _prop_relation_id(page: dict, prop: str) -> str | None:
    rel = page.get("properties", {}).get(prop, {}).get("relation", [])
    return rel[0]["id"] if rel else None


def _prefetch_indexes(
    notion_token: str,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Scan all three DBs and build in-memory dedup indexes.

    Raises DedupGuardError if any scan fails (error != empty).
    """
    try:
        song_pages = _query_all_pages(notion_token, SONGS_DB_ID)
    except SolError as exc:
        raise DedupGuardError(f"Songs pre-fetch failed — aborting: {exc}") from exc

    try:
        playlist_pages = _query_all_pages(notion_token, PLAYLISTS_DB_ID)
    except SolError as exc:
        raise DedupGuardError(f"Playlists pre-fetch failed — aborting: {exc}") from exc

    try:
        junction_pages = _query_all_pages(notion_token, PLAYLIST_TRACKS_DB_ID)
    except SolError as exc:
        raise DedupGuardError(f"Junction pre-fetch failed — aborting: {exc}") from exc

    songs_by_uri: dict[str, str] = {
        uri: p["id"]
        for p in song_pages
        if (uri := _prop_rich_text(p, "Spotify Track URI"))
    }
    playlists_by_id: dict[str, str] = {
        pid: p["id"]
        for p in playlist_pages
        if (pid := _prop_rich_text(p, "Spotify Playlist ID"))
    }
    # Reconstruct each junction's logical key f"{pl_id}|{uri}" from its Song +
    # Playlist RELATIONS, not its Name. The Name is no longer a stable dedup key
    # (sol-enrich Phase A rewrites it to a human-readable "Track · Playlist"),
    # so keying on Name caused sync to re-create every junction. Relations are stable.
    uri_by_song_page = {pid: uri for uri, pid in songs_by_uri.items()}
    plid_by_playlist_page = {pid: plid for plid, pid in playlists_by_id.items()}

    junction_keys: set[str] = set()
    for p in junction_pages:
        uri = uri_by_song_page.get(_prop_relation_id(p, "Song"))
        plid = plid_by_playlist_page.get(_prop_relation_id(p, "Playlist"))
        if uri and plid:
            junction_keys.add(f"{plid}|{uri}")

    return songs_by_uri, playlists_by_id, junction_keys


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _normalize_date(s: str | None) -> str | None:
    if not s:
        return None
    if len(s) == 4:   # "YYYY"
        return f"{s}-01-01"
    if len(s) == 7:   # "YYYY-MM"
        return f"{s}-01"
    return s          # "YYYY-MM-DD" already ISO


def _print_summary(stats: dict, dry_run: bool, update_songs: bool) -> None:
    mode = "DRY RUN" if dry_run else ("COMMIT+UPDATE-SONGS" if update_songs else "COMMIT")
    print(f"\n{'='*40}")
    print(f"  Spotify→Notion Sync — {mode}")
    print(f"{'='*40}")
    print(f"  Playlists seen:        {stats['playlists_seen']}")
    print(f"  With items:            {stats['playlists_with_items']}")
    print(f"  Metadata-only (403/∅): {stats['playlists_metadata_only']}")
    print(f"  Songs created:         {stats['songs_created']}")
    print(f"  Songs updated:         {stats['songs_updated']}")
    print(f"  Songs skipped (exist): {stats['songs_updated_skipped']}")
    print(f"  Junction rows added:   {stats['junction_created']}")
    print(f"  Items skipped:         {stats['items_skipped']}")
    if stats["errors"]:
        print(f"  ERRORS:                {stats['errors']}")
    if dry_run:
        print("\n  No writes made. Rerun with --commit to execute.")
    print(f"{'='*40}\n")


# ---------------------------------------------------------------------------
# Core sync — injected creds, no os.environ
# ---------------------------------------------------------------------------

def run_sync(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    notion_token: str,
    dry_run: bool = True,
    update_songs: bool = False,
) -> dict:
    """Spotify→Notion playlist sync. Takes injected creds — never reads os.environ.

    Returns the stats dict.
    Raises DedupGuardError if any pre-fetch scan returns an error (dedup guard).
    """
    mode = "DRY RUN" if dry_run else ("COMMIT+UPDATE-SONGS" if update_songs else "COMMIT")
    log.info("=== Spotify→Notion sync [%s] ===", mode)

    # 1. Token
    log.info("Refreshing Spotify token...")
    token = refresh_spotify_token(client_id, client_secret, refresh_token)

    # 2. Playlists
    log.info("Fetching playlists...")
    playlists = get_current_user_playlists(token)
    log.info("Found %d playlists", len(playlists))

    # 3. Pre-fetch indexes (dedup guard: errors abort, not pass through)
    log.info("Pre-fetching Notion indexes...")
    songs_by_uri, playlists_by_id, junction_keys = _prefetch_indexes(notion_token)
    log.info(
        "  Existing: %d songs  |  %d playlists  |  %d junction rows",
        len(songs_by_uri), len(playlists_by_id), len(junction_keys),
    )

    # 4. Sync — collect junction tasks then flush concurrently per playlist
    now_iso = datetime.now(timezone.utc).isoformat()
    stats = {
        "playlists_seen": len(playlists),
        "playlists_with_items": 0,
        "playlists_metadata_only": 0,
        "songs_created": 0,
        "songs_updated_skipped": 0,
        "songs_updated": 0,
        "junction_created": 0,
        "items_skipped": 0,
        "errors": 0,
    }

    for pl_idx, playlist in enumerate(playlists, 1):
        pl_id: str = playlist["id"]
        pl_name: str = playlist.get("name", "")
        images = playlist.get("images") or []
        cover_url: str | None = images[0]["url"] if images else None
        description: str = playlist.get("description") or ""

        pl_props = {
            "Playlist Name": title(pl_name),
            "Spotify Playlist ID": rich_text(pl_id),
            "Description": rich_text(description),
            "Last Synced": date(now_iso),
        }

        existing_pl_page_id = playlists_by_id.get(pl_id)
        action = "UPDATE" if existing_pl_page_id else "CREATE"

        if dry_run:
            log.info("[DRY RUN] %s playlist %d/%d: %s", action, pl_idx, len(playlists), pl_name)
            playlist_page_id = existing_pl_page_id or f"<new:{pl_id}>"
        else:
            log.info("%s playlist %d/%d: %s", action, pl_idx, len(playlists), pl_name)
            playlist_page_id = _write_page(
                notion_token,
                PLAYLISTS_DB_ID,
                pl_props,
                page_id=existing_pl_page_id,
                cover_url=cover_url,
            )
            playlists_by_id[pl_id] = playlist_page_id

        items = get_playlist_items(token, pl_id)
        if not items:
            stats["playlists_metadata_only"] += 1
            continue
        stats["playlists_with_items"] += 1

        # --- Songs: sequential (order matters for page_id capture) ---
        song_page_ids: dict[str, str] = {}  # uri -> page_id for this playlist's tracks

        for item in items:
            # /playlists/{id}/items wraps track data under "item", not "track"
            # ("track" inside is a boolean flag distinguishing tracks from episodes)
            track = item.get("item") if item else None
            if track is None:
                stats["items_skipped"] += 1
                continue

            uri: str | None = track.get("uri")
            if not uri:
                stats["items_skipped"] += 1
                continue

            existing_song_page_id = songs_by_uri.get(uri)
            is_new_song = existing_song_page_id is None

            if dry_run:
                song_page_id = existing_song_page_id or f"<new:{uri}>"
                if is_new_song:
                    songs_by_uri.setdefault(uri, song_page_id)
            elif is_new_song:
                song_props = {
                    "Track Name": title(track.get("name", "")),
                    "Artist(s)": rich_text(
                        ", ".join(a["name"] for a in (track.get("artists") or []))
                    ),
                    "Album": rich_text((track.get("album") or {}).get("name", "")),
                    "Duration (ms)": number(track.get("duration_ms")),
                    "Popularity": number(track.get("popularity")),
                    "Spotify Track URI": rich_text(uri),
                    "Release Date": date(
                        _normalize_date((track.get("album") or {}).get("release_date"))
                    ),
                }
                song_page_id = _write_page(
                    notion_token,
                    SONGS_DB_ID,
                    song_props,
                )
                songs_by_uri[uri] = song_page_id
            elif update_songs:
                song_props = {
                    "Track Name": title(track.get("name", "")),
                    "Artist(s)": rich_text(
                        ", ".join(a["name"] for a in (track.get("artists") or []))
                    ),
                    "Album": rich_text((track.get("album") or {}).get("name", "")),
                    "Duration (ms)": number(track.get("duration_ms")),
                    "Popularity": number(track.get("popularity")),
                    "Spotify Track URI": rich_text(uri),
                    "Release Date": date(
                        _normalize_date((track.get("album") or {}).get("release_date"))
                    ),
                }
                song_page_id = _write_page(
                    notion_token,
                    SONGS_DB_ID,
                    song_props,
                    page_id=existing_song_page_id,
                )
            else:
                song_page_id = existing_song_page_id  # type: ignore[assignment]

            song_page_ids[uri] = song_page_id  # type: ignore[assignment]

            if is_new_song:
                stats["songs_created"] += 1
            elif update_songs:
                stats["songs_updated"] += 1
            else:
                stats["songs_updated_skipped"] += 1

        # --- Junction: dedup on composite key f"{pl_id}|{uri}", concurrent POST ---
        # jkey added to junction_keys immediately (before submit) so duplicate tracks
        # in the same playlist can't race through the check and both get written.
        # Using a dict keyed by jkey deduplicates any repeated tracks within a playlist.
        junction_tasks: dict[str, dict] = {}  # jkey -> props
        for idx, item in enumerate(items):
            track = item.get("item") if item else None
            if not track:
                continue
            uri = track.get("uri")
            if not uri:
                continue
            jkey = f"{pl_id}|{uri}"
            if jkey in junction_keys:
                continue
            spid = song_page_ids.get(uri)
            if not spid:
                continue
            # Claim the key now — prevents double-submit within this playlist
            junction_keys.add(jkey)
            junction_tasks[jkey] = {
                # Human-readable Name to match sol-enrich Phase A. Dedup no longer
                # depends on this string — it keys on the Song+Playlist relations.
                "Name": title(f"{track.get('name', '')} · {pl_name}"),
                "Song": relation([spid]),
                "Playlist": relation([playlist_page_id]),
                "Position": number(idx),
                "Added Date": date(item.get("added_at")),
            }
            stats["junction_created"] += 1

        if not dry_run and junction_tasks:
            def _post_junction(
                jkey: str, props: dict, nt: str = notion_token
            ) -> None:
                _write_page(nt, PLAYLIST_TRACKS_DB_ID, props)

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {
                    pool.submit(_post_junction, jk, props): jk
                    for jk, props in junction_tasks.items()
                }
                for fut in as_completed(futures):
                    jk = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        log.error("Junction write failed [%s]: %s", jk, exc)
                        stats["errors"] += 1
                        stats["junction_created"] -= 1
                stats["junction_created"] += 1

    return stats


# ---------------------------------------------------------------------------
# Local CLI entry point — reads shell env, delegates to run_sync
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spotify→Notion playlist sync. Default: dry run."
    )
    parser.add_argument("--commit", action="store_true", help="Write to Notion")
    parser.add_argument(
        "--update-songs",
        action="store_true",
        help="Also PATCH existing songs (default: skip — only create new)",
    )
    args = parser.parse_args()
    dry_run = not args.commit
    update_songs = args.update_songs

    client_id = os.environ.get(ENV_CLIENT_ID)
    client_secret = os.environ.get(ENV_CLIENT_SECRET)
    refresh_token = os.environ.get(ENV_REFRESH_TOKEN)
    notion_token = os.environ.get(ENV_NOTION)

    missing = [
        name
        for name, val in [
            (ENV_CLIENT_ID, client_id),
            (ENV_CLIENT_SECRET, client_secret),
            (ENV_REFRESH_TOKEN, refresh_token),
            (ENV_NOTION, notion_token),
        ]
        if not val
    ]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    stats = run_sync(
        client_id=client_id,  # type: ignore[arg-type]
        client_secret=client_secret,  # type: ignore[arg-type]
        refresh_token=refresh_token,  # type: ignore[arg-type]
        notion_token=notion_token,  # type: ignore[arg-type]
        dry_run=dry_run,
        update_songs=update_songs,
    )
    _print_summary(stats, dry_run, update_songs)


if __name__ == "__main__":
    main()
