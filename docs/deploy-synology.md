# Deploying sail-soon on a Synology NAS

Tested against **DSM 7.2** with the **Container Manager** package. If you're on
DSM 7.1 or earlier you'll have the older Docker package — the commands are the
same, only the GUI screens differ.

## 0. Decide how friends will reach it

Pick one before you start; the rest of the guide branches on this:

- **Tailscale (simpler, private).** You and friends each install Tailscale; the
  NAS is reachable at `http://sail-soon.<tailnet>.ts.net:8000`. Nothing on the
  public internet. Works great for ICS subscriptions because Google Calendar
  fetches from Google's servers — so for **public ICS sharing you still need
  option B** (or Tailscale Funnel, which is basically option B anyway).
- **DSM Reverse Proxy + DDNS + Let's Encrypt (public URL).** Friends get a
  real `https://sail.example.com` they can paste into Google Calendar. Slightly
  more setup but this is what you want if ICS subscription is the goal.

## 1. Prepare directories on the NAS

SSH into the NAS (Control Panel → Terminal & SNMP → enable SSH) or use File
Station.

```bash
ssh admin@<nas-ip>
sudo mkdir -p /volume1/docker/sail-soon
sudo chown $USER:users /volume1/docker/sail-soon
cd /volume1/docker/sail-soon
```

Everything Synology-related lives under `/volume1/docker/` by convention —
Container Manager's default bind-mount root.

## 2. Get the code onto the NAS

Easiest is `git clone` over SSH:

```bash
git clone https://github.com/kwhbitpro/sail-soon.git .
```

If you don't have `git` available, install the **Git Server** package from
Package Center, or just upload the repo folder via File Station.

## 3. Activate the Synology compose override

The repo ships `docker-compose.synology.yml` with the NAS-specific bits
(persistent volume under `/volume1/docker/...`, API bound to loopback for a
reverse proxy). Copy it into place and **pre-create the Postgres data
directory** — bind mounts don't auto-create paths, and Postgres needs the
right ownership on first start:

```bash
cd /volume1/docker/sail-soon
cp docker-compose.synology.yml docker-compose.override.yml
$EDITOR docker-compose.override.yml    # set SAILSOON_HTTP_USER_AGENT to your email

# Pre-create the pgdata dir with correct ownership for postgres:16-alpine (uid 70)
sudo mkdir -p /volume1/docker/sail-soon/pgdata
sudo chown 70:70 /volume1/docker/sail-soon/pgdata
```

> Port **8765** is a free choice — pick anything that doesn't clash with
> another DSM service. DSM itself uses 5000/5001, Photos uses 6000, etc.
>
> If you want the API reachable from your LAN directly (no reverse proxy),
> drop the `127.0.0.1:` prefix from the port mapping.

## 4. Build and start (Container Manager GUI)

1. Open **Container Manager** → **Project** → **Create**.
2. Project name: `sail-soon`.
3. Path: `/volume1/docker/sail-soon`.
4. Source: **Use existing docker-compose.yml**. Container Manager will pick up
   the override file automatically.
5. Click **Next** → **Next** → **Done**. It builds the image and starts the
   three containers (`db`, `api`, `ingest`).

Or via SSH:

```bash
cd /volume1/docker/sail-soon
sudo docker compose up -d --build
```

First build takes a few minutes (installs Python deps). Subsequent restarts
are instant.

## 5. Smoke-test

From the NAS itself:

```bash
curl http://127.0.0.1:8765/health
# {"status":"ok"}

curl 'http://127.0.0.1:8765/summary/today?location=kings_point'
```

The `ingest` container runs `sailsoon ingest-all` once at boot and then every
hour. Give it ~30 seconds on first boot before the scoring endpoints return
data.

Check logs if something's off:

```bash
sudo docker compose logs --tail=50 api
sudo docker compose logs --tail=50 ingest
```

## 6A. Share via Tailscale

1. Install **Tailscale** from Synology Package Center, sign in.
2. Give each friend an invite to your tailnet (free plan covers 3 users).
3. In Tailscale admin → **Machines** → find the NAS → enable **MagicDNS**.
4. Friends hit `http://<nas-hostname>.<tailnet>.ts.net:8765/docs`.
5. For the ICS feed in Google Calendar: Google can't reach Tailscale-private
   URLs, so either (a) use Tailscale **Funnel** to publish just `/calendar.ics`
   publicly, or (b) go to option 6B.

## 6B. Share via DSM Reverse Proxy + HTTPS

### Step-by-step

1. **DDNS:** Control Panel → External Access → DDNS → **Add**. Synology
   provides free `*.synology.me` hostnames, or bring your own domain and add
   it as a CNAME/A to your router's WAN IP.

2. **Router port-forwards:** forward both **TCP 80** and **TCP 443** from your
   router's WAN to the NAS LAN IP. Port 80 is required for the Let's Encrypt
   HTTP-01 ACME challenge — without it the cert request fails.
   Leave DSM ports 5000/5001 **not** forwarded.

3. **Let's Encrypt cert:** Control Panel → Security → Certificate → **Add** →
   **Add a new certificate** → **Get a certificate from Let's Encrypt**. Enter
   your DDNS/custom hostname. DSM will spin up a temporary HTTP listener on
   port 80 to complete the challenge; it renews automatically every 90 days.

4. **HTTPS reverse-proxy rule:** Control Panel → Login Portal → Advanced →
   Reverse Proxy → **Create**:
   | Field | Value |
   |-------|-------|
   | Description | sail-soon |
   | Source Protocol | HTTPS |
   | Source Hostname | `sail.example.com` (your actual hostname) |
   | Source Port | 443 |
   | Destination Protocol | HTTP |
   | Destination Hostname | localhost |
   | Destination Port | 8765 |

   Then open the **Custom Header** tab and click **Create → WebSocket**. This
   adds `Upgrade` and `Connection` headers — required for the `/docs` live-
   reload to work correctly over HTTPS.

5. **HTTP → HTTPS redirect rule:** Add a second reverse-proxy rule so plain
   HTTP visitors are redirected automatically:
   | Field | Value |
   |-------|-------|
   | Source Protocol | HTTP |
   | Source Hostname | `sail.example.com` |
   | Source Port | 80 |
   | Destination Protocol | HTTPS |
   | Destination Hostname | sail.example.com |
   | Destination Port | 443 |

   > DSM's "Redirect" mode is not exposed in the reverse-proxy UI; the
   > destination pointing to the same host on 443 causes DSM to issue a
   > 301 redirect in practice.

6. **Assign the cert:** Control Panel → Security → Certificate → **Settings**.
   In the dropdown next to your DDNS hostname, select the Let's Encrypt cert
   you just created. Also set it as the default if you want it to cover
   requests that don't match an explicit hostname.

7. **Firewall:** Control Panel → Security → Firewall → **Edit Rules** for the
   WAN interface:
   - Allow TCP port 80 (ACME renewal)
   - Allow TCP port 443 (HTTPS)
   - Deny all other ports from WAN (keep DSM 5000/5001 closed externally)

### Verify the setup

```bash
# From your laptop / phone, not the NAS itself:
curl -v https://sail.example.com/health
# Expect: {"status":"ok"}  with TLS handshake shown in -v output

# Confirm HTTP redirects to HTTPS:
curl -I http://sail.example.com/health
# Expect: HTTP/1.1 301 (or 302) with Location: https://...

# Check the cert is valid and issued by Let's Encrypt:
curl -v https://sail.example.com/health 2>&1 | grep -E 'issuer|subject'
```

Now `https://sail.example.com/calendar.ics?profile=brian-dinghy` is the URL
friends paste into Google Calendar → **Other calendars → + → From URL**.

## 7. Creating profiles for friends

Once HTTPS is up:

```bash
curl -X POST https://sail.example.com/profiles \
  -H 'content-type: application/json' \
  -d '{
    "id": "brian-dinghy",
    "name": "Brian dinghy",
    "config": {
      "locations": ["kings_point", "new_haven"],
      "min_verdict": "maybe",
      "include_tides": true,
      "rule_overrides": {
        "wind_speed": {"low_zero": 4, "low_full": 7, "high_full": 15, "high_zero": 20}
      }
    }
  }'
# => {"id":"brian-dinghy", ..., "edit_token":"SAVE-THIS-LOCALLY"}
```

Share `https://sail.example.com/calendar.ics?profile=brian-dinghy` with Brian;
keep the `edit_token` somewhere you can find it (1Password, a pinned note).

## 8. Upgrades

```bash
cd /volume1/docker/sail-soon
git pull
sudo docker compose up -d --build
# Run any new migrations:
sudo docker compose exec api alembic upgrade head
```

## 9. Backup

The only stateful thing is Postgres, in `/volume1/docker/sail-soon/pgdata`.
Add that path to your regular **Hyper Backup** job. Profile definitions and
edit-token hashes live in the DB, so losing it means friends have to re-create
profiles (not the end of the world, but nice to avoid).

For a one-off snapshot:

```bash
sudo docker compose exec db pg_dump -U sailsoon sailsoon \
  > /volume1/docker/sail-soon/backup-$(date +%F).sql
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `api` container restart-looping with `connection refused` to `db` | Postgres still starting. The compose healthcheck should handle it; if it persists, check `docker compose logs db`. |
| `403 Forbidden` from NOAA | Set `SAILSOON_HTTP_USER_AGENT` to include your email. NWS rejects generic UAs. |
| Empty `/conditions` response | `ingest` hasn't run yet. `sudo docker compose exec api sailsoon ingest-all` to trigger manually. |
| Google Calendar not refreshing ICS | Google polls on its own (hours-long) schedule. You can force re-fetch by unsubscribing and resubscribing, but normal drift is expected. |
| Reverse-proxy gives 502 | Port mismatch — override file maps to `8765`; check the proxy destination. |
| Let's Encrypt cert request fails | Port 80 not forwarded on your router, or the DDNS hostname doesn't resolve to your WAN IP yet (wait a few minutes after adding DDNS). |
| Browser shows "certificate not trusted" / NET::ERR_CERT_AUTHORITY_INVALID | Let's Encrypt cert not assigned to the reverse-proxy hostname. Go to Control Panel → Security → Certificate → Settings and set it explicitly. |
| `/docs` WebSocket errors over HTTPS | Missing WebSocket custom headers on the reverse-proxy rule. Open the rule, Custom Header tab, click Create → WebSocket. |
