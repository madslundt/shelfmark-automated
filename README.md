# shelfmark-automated

Syncs your "Want to Read" lists from **Hardcover** and **Goodreads** with your **Calibre-Web Automated (CWA)** library and automatically queues missing books for download in **Shelfmark**.

## How it works

1. Fetches your "Want to Read" books from Hardcover (GraphQL) and Goodreads (RSS)
2. Checks CWA via OPDS to skip books you already own
3. Submits missing books to Shelfmark for download
4. Sleeps and repeats on a configurable interval

```
Hardcover ──┐
            ├──▶ deduplicate ──▶ CWA check ──▶ Shelfmark request
Goodreads ──┘
```

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
  - SYNC_INTERVAL_SECONDS=3600
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
      - SYNC_INTERVAL_SECONDS=3600
      - LOG_LEVEL=INFO
    networks:
      - books_network

networks:
  books_network:
    driver: bridge
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
| `SYNC_INTERVAL_SECONDS` | No | `3600` | Seconds between sync passes. Set to `0` to run once and exit. |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Project structure

```
shelfmark-automated/
├── src/
│   ├── models.py       # Book dataclass + normalisation
│   ├── hardcover.py    # Hardcover GraphQL client
│   ├── goodreads.py    # Goodreads RSS parser
│   ├── cwa.py          # CWA OPDS library checker
│   └── shelfmark.py    # Shelfmark API client (session auth + metadata search)
├── main.py             # Entry point: config, sync loop, deduplication
├── pyproject.toml      # uv project (Python >=3.14)
├── .python-version     # Pins Python 3.14
├── .env.example        # Local development template
├── Dockerfile          # python:3.14-slim + uv
└── docker-compose.yml  # Docker deployment with all environment variables
```
