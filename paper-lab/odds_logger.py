import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


API_KEY = os.environ.get("ODDS_API_KEY")

BASE_URL = "https://api.the-odds-api.com/v4"

# v0.1 deliberately starts cheaply:
# UK bookmakers + H2H only.
REGIONS = "uk"
MARKETS = "h2h"
ODDS_FORMAT = "decimal"

DATA_DIR = Path(__file__).resolve().parent / "data"
LONDON = ZoneInfo("Europe/London")


def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError(
            "ODDS_API_KEY is missing. Store it as a GitHub Actions secret."
        )

    query = dict(params or {})
    query["apiKey"] = API_KEY

    url = f"{BASE_URL}{endpoint}?{urllib.parse.urlencode(query)}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "blood.twin-paper-lab/0.1"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

        quota = {
            "requests_last": response.headers.get("x-requests-last"),
            "requests_used": response.headers.get("x-requests-used"),
            "requests_remaining": response.headers.get("x-requests-remaining"),
        }

    return body, quota


def get_active_sports():
    # /sports does not consume odds quota.
    sports, quota = api_get("/sports/")
    return [sport for sport in sports if sport.get("active")], quota


def get_odds(sport_key):
    return api_get(
        f"/sports/{urllib.parse.quote(sport_key, safe='')}/odds/",
        {
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": "iso",
        },
    )


def flatten_events(events, observed_utc, observed_london):
    rows = []

    for event in events:
        commence_time = event.get("commence_time")

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    rows.append(
                        {
                            "observed_at_utc": observed_utc,
                            "observed_at_london": observed_london,
                            "event_id": event.get("id"),
                            "sport_key": event.get("sport_key"),
                            "sport_title": event.get("sport_title"),
                            "commence_time": commence_time,
                            "home_team": event.get("home_team"),
                            "away_team": event.get("away_team"),
                            "bookmaker_key": bookmaker.get("key"),
                            "bookmaker_title": bookmaker.get("title"),
                            "bookmaker_last_update": bookmaker.get("last_update"),
                            "market": market.get("key"),
                            "outcome": outcome.get("name"),
                            "price_decimal": outcome.get("price"),
                            "point": outcome.get("point"),
                        }
                    )

    return rows


def save_snapshot(rows, metadata):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now_london = datetime.now(LONDON)
    day = now_london.strftime("%Y-%m-%d")
    stamp = now_london.strftime("%Y%m%dT%H%M%S%z")

    csv_path = DATA_DIR / f"odds-{day}.csv"
    metadata_path = DATA_DIR / f"run-{stamp}.json"

    fieldnames = [
        "observed_at_utc",
        "observed_at_london",
        "event_id",
        "sport_key",
        "sport_title",
        "commence_time",
        "home_team",
        "away_team",
        "bookmaker_key",
        "bookmaker_title",
        "bookmaker_last_update",
        "market",
        "outcome",
        "price_decimal",
        "point",
    ]

    file_exists = csv_path.exists()

    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerows(rows)

    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)

    return csv_path, metadata_path


def main():
    observed = datetime.now(timezone.utc)
    observed_utc = observed.isoformat()
    observed_london = observed.astimezone(LONDON).isoformat()

    sports, sports_quota = get_active_sports()

    print(f"Active sports discovered: {len(sports)}")
    print("Beginning controlled v0.1 collection.")

    # IMPORTANT:
    # v0.1 intentionally queries only ONE active sport.
    # This lets us measure real API credit consumption before scaling up.
    if not sports:
        print("No active sports returned.")
        return

    sport = sports[0]

    print(
        f"Test sport: {sport.get('title')} "
        f"({sport.get('key')})"
    )

    events, odds_quota = get_odds(sport["key"])

    rows = flatten_events(
        events,
        observed_utc,
        observed_london,
    )

    metadata = {
        "paper_only": True,
        "version": "0.1",
        "observed_at_utc": observed_utc,
        "observed_at_london": observed_london,
        "regions": REGIONS,
        "markets": MARKETS,
        "odds_format": ODDS_FORMAT,
        "sport": sport,
        "events_returned": len(events),
        "rows_recorded": len(rows),
        "quota_after_sports_request": sports_quota,
        "quota_after_odds_request": odds_quota,
    }

    csv_path, metadata_path = save_snapshot(rows, metadata)

    print(f"Events returned: {len(events)}")
    print(f"Odds rows recorded: {len(rows)}")
    print(f"CSV: {csv_path}")
    print(f"Metadata: {metadata_path}")

    print(
        "API credits — "
        f"last: {odds_quota['requests_last']}, "
        f"used: {odds_quota['requests_used']}, "
        f"remaining: {odds_quota['requests_remaining']}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Paper Lab logger failed: {exc}", file=sys.stderr)
        sys.exit(1)
