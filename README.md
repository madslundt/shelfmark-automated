# shelfmark-automated

Syncs your **Hardcover** and **Goodreads** reading lists with your **Calibre-Web Automated (CWA)** library: queues missing "Want to Read" books for download in **Shelfmark**, and marks fully-read books as read in CWA.

## How it works

**Download sync** (every 2–15 minutes by default):

1. Fetches your "Want to Read" books from Hardcover (GraphQL) and Goodreads (RSS)
2. Skips books already handled in a previous run *(incremental mode — see below)*
3. Checks CWA via OPDS to skip books you already own
4. Submits missing books to Shelfmark for download
5. Sleeps a random interval and repeats

```
Hardcover ──┐
            ├──▶ deduplicate ──▶ state filter ──▶ CWA check ──▶ Shelfmark request
Goodreads ──┘
```

Once a day (configurable) a full verification pass re-checks all books regardless of state,
so nothing is permanently missed if a request fails or a book gets removed from your library.

**Read status sync** (once a day by default — see [Read status sync](#read-status-sync)):

1. Fetches your fully-read books from Hardcover (status: Read) and Goodreads (`shelf=read`)
2. Skips books whose read status was already synced in a previous run
3. Finds each book in CWA via OPDS title/author matching
4. Marks matched books as read in CWA via its web session API

```
Hardcover ──┐
            ├──▶ deduplicate ──▶ state filter ──▶ CWA lookup ──▶ CWA mark as read
Goodreads ──┘
```

Only **fully completed** reads are synced — currently-reading / in-progress books are never touched.

---

## Prerequisites

- Docker + Docker Compose, **or** Python 3.14 + [uv](https://docs.astral.sh/uv/)
- A running [Shelfmark](https://github.com/calibrain/shelfmark) instance
- A running [Calibre-Web Automated](https://github.com/crocodilestick/Calibre-Web-Automated) instance
- A [Goodreads RSS URL](https://www.goodreads.com/review/list_rss/<your_user_id>)
- A [Hardcover API key](https://hardcover.app/account/api) *(optional — Goodreads-only sync works without it)*

---

## Running with Docker

### 1. Configure `docker-compose.yml`

Open `docker-compose.yml` and fill in your values under `environment`:

```yaml
environment:
  - HARDCOVER_API_KEY=your_hardcover_bearer_token
  - GOODREADS_RSS_URL=https://www.goodreads.com/review/list_rss/YOUR_USER_ID
  - CWA_URL=http://192.168.1.100:8083    # use IP, not homeassistant.local — see note
  - CWA_USERNAME=your_cwa_username
  - CWA_PASSWORD=your_cwa_password
  - SHELFMARK_URL=http://192.168.1.100:8084
  - SHELFMARK_USERNAME=your_shelfmark_username  # leave blank if AUTH_METHOD=none
  - SHELFMARK_PASSWORD=your_shelfmark_password
  - SYNC_INTERVAL_MIN_SECONDS=120          # random lower bound (default 2 min)
  - SYNC_INTERVAL_MAX_SECONDS=900          # random upper bound (default 15 min)
  - STATE_FILE=/data/state.db              # persist incremental state (mount volume at /data)
  - READ_STATUS_SYNC_INTERVAL_SECONDS=86400  # mark read books in CWA once a day (0 to disable)
  - PUID=0                                 # UID to run as (0 = root, needed if /data mount is root-owned)
  - PGID=0                                 # GID to run as
```

> **Note on `.local` hostnames:** `homeassistant.local` and similar mDNS hostnames
> do not resolve inside Docker containers. Use the numeric IP address instead.
> On macOS you can find it with:
> ```bash
> dns-sd -G v4 homeassistant.local
> ```

### 2. Build and start

```bash
docker compose up -d
```

### 3. View logs

```bash
docker logs -f shelfmark-automated
```

### Stop

```bash
docker compose down
```

### Networking note

If Shelfmark and CWA are on a shared Docker network, you can use their service names
as hostnames instead of IP addresses. Uncomment the `networks` block in
`docker-compose.yml` and set the network name to match your existing stack:

```yaml
environment:
  - CWA_URL=http://cwa:8083
  - SHELFMARK_URL=http://shelfmark:8084

networks:
  media_network:
    external: true
```

---

## Full stack example

If you want to run CWA, Shelfmark, and this bridge together, use a single
`docker-compose.yml` like the one below. All three services share an internal
network, so you can use service names (`cwa`, `shelfmark`) as hostnames — no
IP addresses needed.

```yaml
version: "3.9"

services:
  # --- Calibre-Web Automated ---
  cwa:
    image: ghcr.io/crocodilestick/calibre-web-automated:latest
    container_name: calibre-web-automated
    environment:
      - PUID=0
      - PGID=0
      - TZ=Europe/Copenhagen
      - DOCKER_MODS=linuxserver/mods:universal-calibre
    volumes:
      - /mnt/data/supervisor/share/books/cwa_config:/config
      - /mnt/data/supervisor/share/books/library:/calibre-library
      - /mnt/data/supervisor/share/books/import:/cwa-book-ingest
    ports:
      - 8083:8083
    restart: unless-stopped
    networks:
      - books_network

  # --- Shelfmark ---
  shelfmark:
    image: ghcr.io/calibrain/shelfmark:latest
    container_name: shelfmark
    environment:
      - PUID=0
      - PGID=0
      - TZ=Europe/Copenhagen
      - INGEST_DIR=/downloads
    volumes:
      - /mnt/data/supervisor/share/books/shelfmark_config:/config
      - /mnt/data/supervisor/share/books/import:/downloads
    ports:
      - 8084:8084
    restart: unless-stopped
    networks:
      - books_network

  # --- shelfmark-automated (this service) ---
  shelfmark-automated:
    image: madslundt/shelfmark-automated:latest
    container_name: shelfmark-automated
    restart: unless-stopped
    depends_on:
      - cwa
      - shelfmark
    environment:
      - HARDCOVER_API_KEY=your_hardcover_bearer_token
      - GOODREADS_RSS_URL=https://www.goodreads.com/review/list_rss/YOUR_USER_ID
      - CWA_URL=http://cwa:8083
      - CWA_USERNAME=your_cwa_username
      - CWA_PASSWORD=your_cwa_password
      - SHELFMARK_URL=http://shelfmark:8084
      - SHELFMARK_USERNAME=your_shelfmark_username
      - SHELFMARK_PASSWORD=your_shelfmark_password
      - SYNC_INTERVAL_MIN_SECONDS=120
      - SYNC_INTERVAL_MAX_SECONDS=900
      - STATE_FILE=/data/state.db
      - READ_STATUS_SYNC_INTERVAL_SECONDS=86400
      - LOG_LEVEL=INFO
      - PUID=0   # set to 0 if your volume mount is root-owned
      - PGID=0
    volumes:
      - shelfmark_state:/data
    networks:
      - books_network

networks:
  books_network:
    driver: bridge

volumes:
  shelfmark_state:
```

> **Shared volume:** Shelfmark writes downloaded books to `/downloads`, which maps
> to the same host path as CWA's `/cwa-book-ingest`. CWA automatically imports
> anything placed there into the Calibre library.

---

## Running locally with Python (for debugging)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you haven't:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Install dependencies

```bash
uv sync
```

### 2. Create a local `.env` file

```bash
cp .env.example .env
# edit .env and fill in your values
```

### 3. Run once (exits after one sync pass — ideal for testing)

```bash
SYNC_INTERVAL_SECONDS=0 uv run --env-file .env python main.py
```

### Run with verbose debug logging

```bash
LOG_LEVEL=DEBUG SYNC_INTERVAL_SECONDS=0 uv run --env-file .env python main.py
```

### Run with only Goodreads (no Hardcover key needed)

```bash
HARDCOVER_API_KEY=dummy SYNC_INTERVAL_SECONDS=0 uv run --env-file .env python main.py
```

---

## Debugging

### Check CWA OPDS is reachable

```bash
curl -u "$CWA_USERNAME:$CWA_PASSWORD" "$CWA_URL/opds"
```

### Search CWA for a specific book title

```bash
curl -u "$CWA_USERNAME:$CWA_PASSWORD" "$CWA_URL/opds/search/dark%20matter"
```

### Check Shelfmark is reachable and see auth mode

```bash
curl "$SHELFMARK_URL/api/health"
curl "$SHELFMARK_URL/api/auth/check"
```

### Test a Shelfmark book request manually

```bash
# 1. Login (skip if AUTH_METHOD=none)
curl -c /tmp/sm.txt -X POST "$SHELFMARK_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASS"}'

# 2. Search metadata to get provider + provider_id
curl -b /tmp/sm.txt "$SHELFMARK_URL/api/metadata/search?query=dark+matter+blake+crouch"

# 3. Submit request using the provider/provider_id from step 2
curl -b /tmp/sm.txt -X POST "$SHELFMARK_URL/api/requests" \
  -H "Content-Type: application/json" \
  -d '{"book_data":{"title":"Dark Matter","author":"Blake Crouch","provider":"hardcover","provider_id":"12345","content_type":"ebook"}}'
```

### Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to resolve 'homeassistant.local'` | mDNS doesn't work in Docker containers | Use IP address instead of `.local` hostname |
| All books show `QUEUE (not in library)` | CWA OPDS title search returning 0 results | Verify CWA credentials; test OPDS curl above |
| `book_data missing required field(s): provider, provider_id` | Shelfmark metadata search failed | Check Shelfmark is reachable; use `LOG_LEVEL=DEBUG` to inspect search results |
| `Maximum pending requests reached` (HTTP 409) | Shelfmark's queue is full | Normal — remaining books are retried on the next sync cycle |
| `Hardcover API key is invalid` (HTTP 401) | Wrong or expired API key | Regenerate key at hardcover.app/account/api |
| `sqlite3.OperationalError: unable to open database file` | `/data` volume is mounted with root-owned permissions; `appuser` can't write | Add `PUID=0` and `PGID=0` to your environment, or ensure the host directory is writable by uid 1000 |

---

## Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `HARDCOVER_API_KEY` | No | — | Hardcover Bearer token. Omit or leave blank to skip Hardcover. |
| `GOODREADS_RSS_URL` | No | — | Goodreads RSS URL (`?shelf=to-read` appended automatically). Omit to skip Goodreads. |
| `CWA_URL` | No | — | CWA base URL. Omit to skip library check (all books queued). |
| `CWA_USERNAME` | No | — | CWA login (leave blank if OPDS is open) |
| `CWA_PASSWORD` | No | — | CWA password |
| `SHELFMARK_URL` | No | — | Shelfmark base URL. Omit to skip download requests. |
| `SHELFMARK_USERNAME` | No | — | Shelfmark login (leave blank if `AUTH_METHOD=none`) |
| `SHELFMARK_PASSWORD` | No | — | Shelfmark password |
| `HARDCOVER_USER_ID` | No | — | Hardcover user ID (logging only) |
| `SYNC_INTERVAL_MIN_SECONDS` | No | `120` | Minimum seconds between sync passes (2 min). |
| `SYNC_INTERVAL_MAX_SECONDS` | No | `900` | Maximum seconds between sync passes (15 min). Each sleep is a random value in this range. |
| `SYNC_INTERVAL_SECONDS` | No | — | Legacy fixed interval. Overrides min/max when set to `N > 0`. Set to `0` to run once and exit. |
| `STATE_FILE` | No | — | Path to the SQLite state DB for incremental sync. Auto-detected as `/data/state.db` when `/data/` exists. |
| `FULL_SYNC_INTERVAL_SECONDS` | No | `86400` | How often (in seconds) to run a full re-check of all books regardless of state. Set to `0` to disable. |
| `READ_STATUS_SYNC_INTERVAL_SECONDS` | No | `86400` | How often (in seconds) to sync read status from Hardcover/Goodreads to CWA. Set to `0` to disable entirely. Requires `CWA_USERNAME` and `CWA_PASSWORD`. |
| `FIX_METADATA` | No | `true` | Automatically correct wrong author metadata in CWA. Runs on the same schedule as read-status sync (both shelves). Set to `false` to disable. Requires **"Edit books"** permission for your CWA user (Admin → Edit User). |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PUID` | No | `1000` | UID the process runs as. Set to `0` if your `/data` volume mount is root-owned. |
| `PGID` | No | `1000` | GID the process runs as. Set to `0` if your `/data` volume mount is root-owned. |

---

## Read status sync

When `CWA_USERNAME` and `CWA_PASSWORD` are set, the service also syncs your read status back into CWA once a day (configurable via `READ_STATUS_SYNC_INTERVAL_SECONDS`).

### What it does

- Fetches books you have marked as **Read** (not currently-reading) from Hardcover and Goodreads
- Finds each book in your CWA library via the same OPDS title/author matching used by the download sync
- Marks matched books as read in CWA using its web session API (`POST /ajax/book/{id}/readstatus`)

### When it runs

| Situation | Behaviour |
|-----------|-----------|
| First container start (no prior timestamp) | Runs immediately |
| Subsequent starts with `STATE_FILE` set | Runs only once the configured interval has elapsed since the last sync |
| `STATE_FILE` not set | Runs every loop iteration (no persistent timing — sets every 2–15 min) |
| `READ_STATUS_SYNC_INTERVAL_SECONDS=0` | Disabled entirely — never runs |

### Logging

At the default `INFO` level you will see:
```
CWA: logged in as 'admin'              # once per session on first read status sync
Read status sync: 3 marked as read, 1 not found in library
```

Set `LOG_LEVEL=DEBUG` for per-book detail:
```
CWA: found 'The Martian' (id=42) via query 'The Martian'
CWA: book 42 marked as read
Read status sync: 'Unknown Title' not found in CWA — skipping
```

### Requirements

- `CWA_USERNAME` and `CWA_PASSWORD` must be set — read status is per-user and requires a logged-in web session (OPDS basic auth alone is not sufficient)
- The CWA web interface must be reachable at `CWA_URL` (same URL used for OPDS)

### Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CWA login failed — check CWA_USERNAME and CWA_PASSWORD` | Wrong credentials | Verify you can log in to the CWA web UI with the same credentials |
| `Read status sync: 'Title' not found in CWA` | Book is not yet in your library | Download it first via the download sync, or add it to CWA manually |
| `Read status sync: 0 marked as read` every run | No books in `shelf=read` / Hardcover Read status | Check your Goodreads read shelf or Hardcover read list is populated |
| Read status not updating despite sync running | CWA session cookie expired mid-run | Next run re-authenticates automatically |

---

## Metadata correction

When `FIX_METADATA=true` (the default), the service automatically corrects wrong author metadata in CWA for books found on Hardcover or Goodreads. It runs on the same schedule as read-status sync.

### What it does

- Fetches books from your **Read** and **Want to Read** shelves on Hardcover and Goodreads
- Searches CWA via OPDS for each book by title
- When a title matches but the stored author differs, it writes the correct author back to CWA via `POST /edit/<id>`

### Prerequisite: enable "Edit books" in CWA

> **Before this feature will work**, you must grant your CWA user the **Edit books** permission:
>
> 1. Log in to CWA as an admin
> 2. Go to **Admin → Edit User** and select your account
> 3. Enable the **Edit books** checkbox and save
>
> Without this permission, `POST /edit/<id>` is rejected and no metadata will be updated.

### When it runs

Runs on the same schedule as `READ_STATUS_SYNC_INTERVAL_SECONDS` (default: once per day). Set `FIX_METADATA=false` to disable it entirely without affecting read-status sync.

### Logging

At the default `INFO` level:
```
Metadata fix: book 42 'All The Lies' — author corrected from 'jennifer harvey' to 'Nicola Sanders'
Metadata fix: 1 corrected, 0 failed
```

Set `LOG_LEVEL=DEBUG` for per-book detail including books where no mismatch was found.

### Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Metadata fix: 0 corrected` every run | No mismatches found, or books not yet in library | Check `LOG_LEVEL=DEBUG` output; confirm books exist in CWA |
| `Metadata fix: failed to update book <id>` | CWA returned HTTP 403 | Enable **Edit books** permission for your CWA user (see prerequisite above) |
| Author not updating despite `200 OK` | Field name mismatch with your CWA version | Set `LOG_LEVEL=DEBUG` and check the form fields in the edit page |

---

## Incremental sync

By default the service runs in stateless mode and re-checks every book on every pass.
Enable incremental sync by setting `STATE_FILE` and mounting a persistent volume:

```yaml
# docker-compose.yml
environment:
  - STATE_FILE=/data/state.db
volumes:
  - shelfmark_state:/data
```

**How it works:**
- First run processes all books as normal.
- Subsequent runs skip books that were already found in the library or successfully submitted.
- Books that failed or had no metadata found are retried automatically.
- Every `FULL_SYNC_INTERVAL_SECONDS` (default 24 h) all books are re-checked regardless of state, as a safety net.

Without a volume mount the state file is stored inside the container and lost on restart (graceful fallback to stateless mode).

> **Volume permission note:** If your `/data` mount is root-owned (common with bind mounts on NAS or Portainer),
> add `PUID=0` and `PGID=0` to your environment so the container runs as root and can write the state file.
> Named Docker volumes (`shelfmark_state:`) are initialised with correct permissions automatically and don't need this.

---

## Project structure

```
shelfmark-automated/
├── src/
│   ├── models.py       # Book dataclass + normalisation
│   ├── hardcover.py    # Hardcover GraphQL client
│   ├── goodreads.py    # Goodreads RSS parser
│   ├── cwa.py          # CWA OPDS library checker + web session client (read status)
│   ├── shelfmark.py    # Shelfmark API client (session auth + metadata search)
│   └── state.py        # SQLite state manager for incremental sync
├── main.py             # Entry point: config, sync loop, deduplication
├── pyproject.toml      # uv project (Python >=3.14)
├── .python-version     # Pins Python 3.14
├── .env.example        # Local development template
├── Dockerfile          # python:3.14-slim + uv + gosu
├── entrypoint.sh       # Fixes /data permissions as root, drops to appuser (or PUID/PGID)
└── docker-compose.yml  # Docker deployment with all environment variables
```
