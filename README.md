# sol-spotify

Apify actor that syncs Spotify playlists into Notion. Reads all playlists from the authenticated Spotify account and writes songs, playlist metadata, and playlist-track junction rows into the Sol OS Notion databases.

## What it does

- Fetches all playlists for the authenticated Spotify account
- Creates or updates Playlist rows in Notion
- Creates Song rows for tracks not yet in the DB (skips existing by Track URI)
- Creates Playlist Track junction rows linking each song to its playlist
- Dedup guard aborts the run if a pre-fetch scan returns unexpected empty results, preventing mass duplicate creation
- Writes a row to the Agent Run Log after every run (success or crash)

## How it fits

```
Spotify API
    ↓
sol-spotify (this repo)
    ↓ writes to
Notion: Songs DB, Playlists DB, Playlist Tracks DB
    ↓ read by
sol-ig, sol-pin (content pipeline)
```

Sync logic lives in `sync.py`. `main.py` is a thin Apify entry point that reads creds from environment variables and calls `run_sync()`. All shared helpers come from SolOSDK.

## Notion databases

| DB | ID |
|---|---|
| Songs | `72df16fe5ba641be8bf8f6cfc81a3445` |
| Playlists | `2becc40c1bb841d58ae2a7de9001f2d7` |
| Playlist Tracks | `36bd18e459784bfda9aa7c8dc00115eb` |

## Environment variables

Set these in Apify Console → actor Settings → Environment Variables. Check the Secret box on each.

| Variable | What it is |
|---|---|
| `solifyid` | Spotify OAuth client ID |
| `solifysec` | Spotify OAuth client secret |
| `resolify` | Spotify refresh token |
| `solinotion` | Notion integration token |

## Actor input

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | boolean | `true` | No writes to Notion — just logs what would happen |
| `update_songs` | boolean | `false` | Re-patch existing song rows (popularity etc.). Off by default — only creates new |

## Running locally

Create `storage/key_value_stores/default/INPUT.json` (gitignored):

```json
{
  "dry_run": true,
  "update_songs": false
}
```

Set env vars in your shell (they live in zshrc as `solifyid`, `solifysec`, `resolify`, `solinotion`), then:

```bash
apify run
```

## Deploying

SolOSDK must be pushed to GitHub before building this actor — the build installs it via `git+https`.

```bash
git add -A && git commit -m "..." && git push
# then in Apify Console → Builds → Start build
# or: apify push
```

## Verification

After any run, check the Apify dataset output and confirm:
- junction rows == distinct `playlist_id|track_uri` pairs (no growth on a re-sync of an already-populated DB)
- Agent Run Log has a new row

Run with `dry_run: true` first. Always run manually once before enabling the cron.

## Schedule

Daily. Full re-sync is heavy on Notion's rate limit — do not run bi-hourly.
