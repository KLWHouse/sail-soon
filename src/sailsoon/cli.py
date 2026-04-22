from __future__ import annotations

import json
from typing import Optional

import typer

from .ingest import ingest_marine, ingest_openmeteo, ingest_tides
from .sync import sync_locations


app = typer.Typer(help="sail-soon ingestion and maintenance CLI.")


@app.command()
def sync() -> None:
    """Write the locations table from config/locations.yml."""
    n = sync_locations()
    typer.echo(json.dumps({"locations_synced": n}))


@app.command("tides")
def tides_cmd(
    location: Optional[list[str]] = typer.Option(None, "--location", "-l"),
    days: int = typer.Option(7, "--days", "-d"),
) -> None:
    counts = ingest_tides(location, days)
    typer.echo(json.dumps({"tide_rows_by_location": counts}))


@app.command("marine")
def marine_cmd(
    location: Optional[list[str]] = typer.Option(None, "--location", "-l"),
) -> None:
    counts = ingest_marine(location)
    typer.echo(json.dumps({"marine_rows_by_location": counts}))


@app.command("weather")
def weather_cmd(
    location: Optional[list[str]] = typer.Option(None, "--location", "-l"),
    days: Optional[int] = typer.Option(None, "--days", "-d"),
) -> None:
    counts = ingest_openmeteo(location, days)
    typer.echo(json.dumps({"weather_rows_by_location": counts}))


@app.command("ingest-all")
def ingest_all(
    location: Optional[list[str]] = typer.Option(None, "--location", "-l"),
    days: int = typer.Option(7, "--days", "-d"),
) -> None:
    """Run everything in the right order: sync → tides → marine → weather.

    Failures in any one subsystem are logged but don't abort the others —
    an NWS outage shouldn't block tide/weather refresh."""
    sync_locations()
    out: dict[str, object] = {}
    for name, fn in [
        ("tides", lambda: ingest_tides(location, days)),
        ("marine", lambda: ingest_marine(location)),
        ("weather", lambda: ingest_openmeteo(location, days)),
    ]:
        try:
            out[name] = fn()
        except Exception as err:  # noqa: BLE001
            out[name] = {"error": f"{type(err).__name__}: {err}"}
    typer.echo(json.dumps(out))


if __name__ == "__main__":
    app()
