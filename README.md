# sail-soon

Aggregates NOAA + Open-Meteo marine data for Long Island Sound, scores each hour
against a configurable "good sailing" rule set, and exposes everything over an
agent-friendly HTTP API.

Answer questions like:

- _"Is tomorrow any good for a sail from Kings Point?"_ → `GET /summary/tomorrow?location=kings_point`
- _"Which window this week should I aim for?"_ → `GET /sail-windows?location=new_london&when=this-week`
- _"What's the raw wind/wave/precip outlook?"_ → `GET /conditions?location=bridgeport&when=today`

## Data sources (all free, no API keys)

| Source | Used for |
|--------|----------|
| NOAA CO-OPS (`api.tidesandcurrents.noaa.gov`) | Tide predictions (hi/lo + hourly) |
| NWS (`api.weather.gov`) | Marine zone forecasts + hazards (Small Craft Advisory etc.) |
| Open-Meteo (`open-meteo.com`) | Hourly wind, gusts, wave height, precip probability, temp, cloud |

## Quick start

### Local dev with SQLite

```bash
pip install -e '.[dev]'
alembic upgrade head
sailsoon sync            # write locations table from config/locations.yml
sailsoon ingest-all      # pull tides + marine + weather for all locations
uvicorn sailsoon.api:app --reload
open http://localhost:8000/docs
```

### Production with Postgres (Lightsail or Synology)

```bash
docker compose up -d --build
```

The `api` container runs migrations, syncs locations, and serves on :8000.
The `ingest` container re-pulls forecasts every hour.

Point Caddy/Traefik/Nginx Proxy Manager at `api:8000` to expose a public URL
(Lightsail) or keep it behind Tailscale (Synology).

## Configuration

Everything tunable is in YAML — no code changes needed.

- `config/locations.yml` — the stations/zones/lat-lons you care about. Add a
  new harbor by copying an existing entry.
- `config/rules.yml` — the scoring rules. Edit thresholds (wind range, gust
  ceiling, wave max, precip) and re-hit the API; no re-ingestion required.

Every rule has a `type`:

- `trapezoid` — ideal band with soft edges (used for wind speed).
- `ceiling` — 1.0 up to `warn_at`, ramps to 0 at `fail_at` (gusts, waves, precip).
- `nws_hazard` — fails the hour if the marine zone has a matching hazard.

A window is "good" when its hourly score stays above `min_score` for at least
`min_duration_hours` consecutive daylight hours.

## API surface (agent-callable)

`GET /openapi.json` has the full schema. Highlights:

| Endpoint | Returns |
|----------|---------|
| `GET /locations` | All configured sailing locations |
| `GET /summary/{when}?location=X` | `{verdict: go \| maybe \| no-go, best_window, windows}` |
| `GET /sail-windows?location=X&when=this-week` | Every contiguous good window, best first |
| `GET /conditions?location=X&when=today` | Hour-by-hour scored forecast |
| `GET /tides?location=X&when=this-week` | High/low tide predictions |
| `GET /marine?location=X` | Raw NWS zone forecasts with extracted hazards |

`when` accepts: `today`, `tomorrow`, `this-week`, or an ISO date `2026-05-10`.

### Example: using it from an agent

Point an LLM with tool use at `http://sail-soon.example.com/openapi.json`. It
will discover the endpoints and can answer free-form questions like _"best
sail window for Kings Point in the next 5 days, assuming I can go any day"_
with a single `/sail-windows` call plus a bit of reasoning on the response.

## Roadmap

- `/calendar.ics` — subscribable ICS feed so friends can add sail windows to
  Google Calendar without OAuth.
- Google Calendar push (OAuth) — optional opinionated push of "go" windows.
- Per-user profiles — different rule sets (dinghy vs cruiser vs racing).
- Historical backfill + tuning — use past scored windows against actual sail
  logs to auto-tune thresholds.
