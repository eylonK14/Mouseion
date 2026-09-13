# Production deployment with Tailscale

This guide deploys Mouseion on a Linux home server or VPS and exposes it only to devices in your Tailscale network. Tailscale Serve is the recommended edge: it terminates browser-trusted HTTPS automatically and preserves tailnet ACLs, while the production Compose file binds Mouseion and Open WebUI to host loopback only.

Do not use Tailscale Funnel for this single-user library. Funnel makes a service reachable from the public internet; Serve keeps it inside the tailnet.

## 1. Prepare the host

Install:

- [Docker Engine and the Compose plugin](https://docs.docker.com/engine/install/) (Engine 24+, Compose v2.20+)
- [Tailscale](https://tailscale.com/kb/1017/install/) on the server, desktop, and phone
- Git and GNU Make

Join the server to the tailnet and note its fully qualified MagicDNS name:

```bash
sudo tailscale up
tailscale status
docker --version
docker compose version
```

Tailscale Serve requires HTTPS certificates enabled for the tailnet. Running the first Serve command opens the consent flow when needed; the Serve daemon then obtains and renews the certificate used at the edge. Device DNS names appear in Certificate Transparency, so rename the server first if its hostname is sensitive. See Tailscale's [Serve guide](https://tailscale.com/docs/features/tailscale-serve) and [HTTPS documentation](https://tailscale.com/docs/how-to/set-up-https-certificates).

## 2. Configure Mouseion

```bash
git clone https://github.com/eylonK14/Mouseion.git /opt/mouseion
cd /opt/mouseion
cp .env.example .env
```

Generate `API_TOKEN`, add the OpenRouter key, and set the browser origins. Replace `mouseion.example-tailnet.ts.net` with the DNS name from `tailscale status`:

```dotenv
API_TOKEN=<a random value from: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'>
OPENROUTER_API_KEY=<your OpenRouter key>
PUBLIC_BASE_URL=https://mouseion.example-tailnet.ts.net
OPENWEBUI_BASE_URL=https://mouseion.example-tailnet.ts.net:8443
OPENROUTER_APP_URL=https://mouseion.example-tailnet.ts.net
BACKUP_HOST_DIR=./backups
```

Keep `.env` mode-restricted and never paste its token into source files:

```bash
chmod 600 .env
mkdir -p backups
sudo chown 10001:10001 backups
```

## 3. Start the hardened stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 migrate api worker
```

The migration service runs automatically and must complete before API/worker startup. Production differences from the development file:

- backend processes run as UID/GID 10001 with `no-new-privileges`
- the library, Redis queue, Hugging Face cache, and Open WebUI state use named volumes
- host ports bind only to `127.0.0.1`
- every service has a health check, restart policy, and resource ceiling
- Redis uses an append-only log so queued work survives normal restarts

Check the local edge before enabling Serve:

```bash
curl -fsS http://127.0.0.1:8000/health
```

## 4. Publish tailnet-only HTTPS

Serve Mouseion on normal HTTPS and make the rule persistent across reboot:

```bash
tailscale serve --bg --https=443 8000
tailscale serve status
```

Open the reported `https://…ts.net` URL from the desktop and phone while both are connected to Tailscale. The PWA/share target requires this secure HTTPS origin; direct `http://100.x.y.z:8000` is not sufficient for installation.

To remove the rule later:

```bash
tailscale serve --https=443 off
```

## 5. Add Open WebUI and the three pipes

```bash
docker compose -f docker-compose.prod.yml --profile webui up -d --build
tailscale serve --bg --https=8443 3000
tailscale serve status
```

Open `https://<server-name>.<tailnet>.ts.net:8443`. On a fresh Open WebUI volume, choose **Create Admin Account**; the first account is the administrator. Then follow [pipes/README.md](pipes/README.md) to create and enable the `paper_library`, `single_paper`, and `test_me` Functions and set these Valves on each:

```text
MOUSEION_API_BASE_URL=http://api:8000
MOUSEION_API_TOKEN=<the API_TOKEN from .env>
```

Functions execute server-side Python. Install only the three reviewed files from this repository; Open WebUI's [official Functions documentation](https://docs.openwebui.com/features/extensibility/plugin/functions/) recommends the same trust boundary.

## 6. First-run checklist

1. Open `https://<server>.<tailnet>.ts.net/`; enter the API token once on the desktop.
2. Add one arXiv link and watch it move through download, extraction, indexing, tagging, embedding, and done.
3. Visit `/admin`; run health again. Confirm DB/FTS/embedding/disk are green, OpenRouter is reachable, and PageIndex is green or an intentional auto-mode warning.
4. In Open WebUI, confirm **Paper Library**, **Single Paper**, and **Test me** appear and ask one grounded question.
5. From desktop Mouseion, visit `/admin` and click **Create pairing QR**.
6. Scan it with the phone. The short-lived link stores the API token in that browser without displaying or typing it.
7. Install Mouseion from the phone browser. Share an arXiv page and then a PDF to Mouseion; each should require only **Share → Add to library**.
8. Configure the nightly backup and run the restore drill in [BACKUPS.md](BACKUPS.md).

## Upgrades and routine checks

```bash
cd /opt/mouseion
git pull --ff-only
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec -T api python -m mouseion.maintenance health-check
```

Review `PROJECT_NOTES.md` before model/dimension changes. Changing only `EMBEDDING_MODEL` keeps old vectors readable but marks them stale; run `make reembed`. Changing `EMBEDDING_DIM` also requires a migration that rebuilds `paper_vectors`.
