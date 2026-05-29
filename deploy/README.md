# Deploying the Winnow daemon

Winnow runs as a **Docker container** (the going-in assumption) — the always-ready
review app (ADR-0007), surviving reboots.

## Docker (the supported path)

```bash
# from the project root (where docker-compose.yml lives)
sudo docker compose up -d --build
```

Then open **http://YOUR_HOST:8077** from any machine on the LAN.

- `restart: unless-stopped` in `docker-compose.yml` brings the container back on
  boot (Docker starts at boot), so no init/service file is needed.
- Set your real values in `docker-compose.override.yml` (copy from
  `docker-compose.override.yml.example`; gitignored — never edit the base file):
  `FRIGATE_URL` must be your **browser-reachable** Frigate address (LAN IP), not
  `127.0.0.1` — image URLs are embedded for the client.
- Model config is **auto-discovered** from Frigate at refresh time (no names are
  hardcoded); add a dog/car classifier in Frigate and it shows up automatically.

## Manual (for testing, no container)

```bash
cd adapters/frigate
FRIGATE_URL=http://YOUR_HOST:5000 FRIGATE_API_PREFIX=/api python3 daemon.py
```

A fully-documented **standalone (non-Docker) install** is on the roadmap — see
[`docs/ROADMAP.md`](../docs/ROADMAP.md).

## Notes
- Needs **Ollama** (`qwen2.5vl:7b`) for the auto-refresh pre-sort; the app still
  serves and accepts swipes without it (refresh just won't classify).
- No auth needed against Frigate's `5000/api` port on a trusted LAN (ADR-0005 /
  docs/SETUP.md). For an authed/remote setup, set `FRIGATE_USER`/`FRIGATE_PASSWORD`
  + an 8971 HTTPS URL (TLS handling still TODO).
- Refresh cadence: `WINNOW_REFRESH_SEC` (default 1800).
