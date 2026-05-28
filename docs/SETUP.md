# Winnow setup (Frigate adapter)

Winnow runs as a companion next to — or on a different box from — your Frigate
install and talks to it over HTTP. The main decision is **which Frigate port**
you point it at; that determines whether you need credentials.

## Prerequisites

- Frigate **0.17+** reachable over the network.
- For optional model-assisted pre-sort: **Ollama** with a vision model
  (`ollama pull qwen2.5vl:7b`) on a box with a spare GPU. *Optional* — without it
  Winnow is a plain bulk reviewer.
- Python 3.11+ (stdlib only; no pip installs for the client).

## Which port? (Frigate exposes three)

| Port | Bind | Path | Auth | Use for |
|------|------|------|------|---------|
| **5000** | `0.0.0.0` | `/api/...` | **none** (no role enforcement, by design) | **recommended** — trusted-LAN companion, local or remote |
| **8971** | `0.0.0.0` | `/api/...` | login token, **HTTPS/TLS** | companion over an untrusted network |
| 5001 | `127.0.0.1` | root (`/...`) | login token | the internal app — loopback only, not for remote |

On a trusted LAN behind a firewall, **5000/api is the intended integration
path** — it's network-reachable and needs no credentials. (This is independent
of whether Frigate's UI auth is enabled; 5000 is the unauthenticated port either
way.) Confirm what your instance exposes:

```bash
python3 adapters/frigate/frigate_client.py probe
```

## Recommended config (5000/api, no creds)

`.env` at the project root (gitignored):

```ini
FRIGATE_URL=http://YOUR_FRIGATE_IP:5000
FRIGATE_API_PREFIX=/api
# no FRIGATE_USER / FRIGATE_PASSWORD needed
```

Verify:
```bash
python3 adapters/frigate/frigate_client.py ping          # version + authed: True
python3 adapters/frigate/frigate_client.py train-list Scooby   # real JSON list
python3 adapters/frigate/frigate_client.py faces             # face library
```

> Security note: 5000/api has **no auth** — anyone who can reach that port on
> your network can read *and* mutate classification data (categorize, delete,
> train). That's fine behind a firewall on a trusted LAN; if that's not your
> situation, use 8971 below.

## Authenticated config (8971/api over TLS — untrusted networks)

The client supports auth (login → token, sent as cookie + Bearer, re-login on
401). Point it at 8971 and supply admin credentials:

```ini
FRIGATE_URL=https://YOUR_FRIGATE_IP:8971
FRIGATE_API_PREFIX=/api
FRIGATE_USER=admin
FRIGATE_PASSWORD=your-password
```

Notes:
- 8971 is **HTTPS** (often a self-signed cert) — TLS/cert handling for this port
  is not wired up yet; use 5000/api on trusted LANs for now.
- **Lost the admin password?** Reset it: add `auth:\n  reset_admin_password: true`
  to Frigate's `config.yml`, `sudo docker restart frigate`, read it from
  `sudo docker logs frigate 2>&1 | grep "Password:"`, set your own in the UI
  (Settings → Users), then **remove that config line** (it re-rolls on every
  restart) and restart again.

## Remote vs local

Winnow doesn't care where Frigate runs — set `FRIGATE_URL` to any reachable
address (LAN IP, Tailscale, etc.). Ollama can live on a different box too; the
two aren't coupled.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Connection refused` on `:5001` | 5001 is loopback-only — use `:5000` with `/api` |
| 5000 root paths return HTML | that's the SPA; the API is under `/api` (set prefix) |
| `:5000/api/*` works without creds | expected — 5000 is unauthenticated |
| `:8971` → 400 over http | 8971 is HTTPS; use `https://` |
| `403 admin role required` | hitting the authed app (5001/8971) without admin login |
| password keeps changing | `reset_admin_password: true` still in config — remove it |
