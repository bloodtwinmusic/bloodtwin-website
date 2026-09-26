import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_URL = "https://api.the-odds-api.com/v4"
REGIONS = "uk"
ODDS_FORMAT = "decimal"
DATA_DIR = Path(__file__).resolve().parent / "data"
LONDON = ZoneInfo("Europe/London")

CSV_V0_1_FIELDNAMES = [
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

CSV_V0_2_FIELDNAMES = [
    "observed_at_utc",
    "observed_at_london",
    "event_id",
    "sport_key",
    "sport_title",
    "commence_time_utc",
    "commence_time_london",
    "home_team",
    "away_team",
    "bookmaker_key",
    "bookmaker_title",
    "bookmaker_last_update",
    "market",
    "outcome",
    "price_decimal",
    "point",
    "raw_implied_probability",
    "market_de_vigged_probability",
    "schema_version",
]

CSV_FIELDNAMES = CSV_V0_2_FIELDNAMES
CSV_SCHEMA_VERSIONS = {"0.1": CSV_V0_1_FIELDNAMES, "0.2": CSV_V0_2_FIELDNAMES}

PAPER_ONLY_SPORTS = []

PAPER_ONLY_SPORT_ALIASES = {
    "soccer": {"soccer"},
    "tennis": {"tennis"},
    "basketball": {"basketball"},
    "american_football": {"american_football", "americanfootball"},
    "baseball": {"baseball"},
    "ice_hockey": {"ice_hockey", "icehockey"},
}


def default_market_keys():
    configured = os.environ.get("ODDS_MARKETS", "h2h")
    markets = [item.strip() for item in configured.split(",") if item.strip()]
    return markets or ["h2h"]


def default_request_budget():
    raw_value = os.environ.get("ODDS_REQUEST_BUDGET", "10")
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 10


def env_int(name, default):
    raw_value = os.environ.get(name, str(default))
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def normalize_sport_name(value):
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", "_")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def sport_is_paper_lab_allowed(sport):
    if not isinstance(sport, dict):
        return False

    sport_key = normalize_sport_name(sport.get("key"))
    sport_group = normalize_sport_name(sport.get("group"))
    sport_title = normalize_sport_name(sport.get("title"))
    candidates = {sport_key, sport_group, sport_title}

    for base, aliases in PAPER_ONLY_SPORT_ALIASES.items():
        for alias in aliases:
            normalized_alias = normalize_sport_name(alias)
            if any(candidate == normalized_alias or candidate.startswith(f"{normalized_alias}_") for candidate in candidates):
                return True
            if base in candidates:
                return True
    return False


def format_utc_timestamp(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def format_london_timestamp(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LONDON).isoformat()


def api_get(endpoint, params=None):
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY is missing. Store it in the environment.")

    query = dict(params or {})
    query["apiKey"] = api_key
    url = f"{BASE_URL}{endpoint}?{urllib.parse.urlencode(query)}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "blood.twin-paper-lab/0.4"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
        quota = {
            "requests_last": response.headers.get("x-requests-last"),
            "requests_used": response.headers.get("x-requests-used"),
            "requests_remaining": response.headers.get("x-requests-remaining"),
            "credits_used": response.headers.get("x-requests-used"),
            "credits_remaining": response.headers.get("x-requests-remaining"),
        }

    return body, quota


def oddsrelay_key_present():
    return bool(os.environ.get("ODDSRELAY_KEY"))


def provider_status():
    return {
        "the_odds_api": bool(os.environ.get("ODDS_API_KEY")),
        "oddsrelay": oddsrelay_key_present(),
    }


def require_provider_credentials():
    status = provider_status()
    missing = [name for name, present in status.items() if not present]
    if missing:
        raise RuntimeError("Missing provider credential(s): " + ", ".join(missing))
    return status


def print_provider_readiness():
    status = provider_status()
    print("Provider readiness: " + ", ".join(f"{name}={'READY' if ready else 'MISSING'}" for name, ready in status.items()))
    return status


ODDSRELAY_BASE_URL = os.environ.get("ODDSRELAY_BASE_URL", "https://api.oddsrelay.io")


def oddsrelay_api_get(endpoint, params=None):
    api_key = os.environ.get("ODDSRELAY_KEY")
    if not api_key:
        raise RuntimeError("ODDSRELAY_KEY is missing. Store it in the environment.")
    query = urllib.parse.urlencode(dict(params or {}))
    url = f"{ODDSRELAY_BASE_URL}{endpoint}" + (f"?{query}" if query else "")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "blood.twin-paper-lab/0.4",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OddsRelay HTTP {exc.code}: {raw_error}") from None
    with response:
        payload = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            import gzip
            payload = gzip.decompress(payload)
        raw = payload.decode("utf-8")
        body = json.loads(raw) if raw else None
        usage = {
            "tokens_cost": response.headers.get("X-Tokens-Cost"),
            "tokens_used": response.headers.get("X-Tokens-Used"),
            "tokens_remaining": response.headers.get("X-Tokens-Remaining"),
            "etag": response.headers.get("ETag"),
        }
    return body, usage


def get_oddsrelay_sports():
    return oddsrelay_api_get("/v2/sports")


def oddsrelay_discover_catalogue():
    endpoints = {
        "sports": "/v2/sports",
        "bookmakers": "/v2/bookmakers",
        "regions": "/v2/regions",
        "usage": "/v2/usage",
        "pricing": "/v2/pricing",
    }
    catalogue = {}
    for name, endpoint in endpoints.items():
        body, usage = oddsrelay_api_get(endpoint)
        catalogue[name] = {"data": body, "usage": usage}
    return catalogue


def oddsrelay_window_params(start_time, end_time, region="uk"):
    return {
        "region": region,
        "commenceTimeFrom": format_utc_timestamp(start_time).replace("+00:00", "Z"),
        "commenceTimeTo": format_utc_timestamp(end_time).replace("+00:00", "Z"),
    }


def oddsrelay_quote_collection_window(start_time, end_time, region="uk"):
    return oddsrelay_quote_catalogue(oddsrelay_window_params(start_time, end_time, region))


def summarize_oddsrelay_preflight(catalogue, quotes):
    summary = {"catalogue": {}, "quotes": {}}
    for name, result in catalogue.items():
        data = result.get("data")
        count = len(data) if isinstance(data, (list, dict)) else None
        summary["catalogue"][name] = {
            "items": count,
            "tokens_cost": result.get("usage", {}).get("tokens_cost"),
        }
    for name, result in quotes.items():
        if "error" in result:
            summary["quotes"][name] = {"status": "ERROR", "error": result["error"]}
        else:
            summary["quotes"][name] = {
                "status": "OK",
                "tokens_cost": result.get("usage", {}).get("tokens_cost"),
                "quote": result.get("quote"),
            }
    return summary


def oddsrelay_choose_products_from_quotes(quotes):
    """Return only quote-validated OddsRelay products; no acquisition occurs here."""
    chosen = []
    for product, result in quotes.items():
        if "error" not in result and "quote" in result:
            chosen.append(product)
    return chosen


def oddsrelay_build_acquisition_plan(start_time, end_time, region="uk"):
    """Zero-purchase planning stage: quote first, then expose validated products."""
    params = oddsrelay_window_params(start_time, end_time, region)
    quotes = oddsrelay_quote_catalogue(params)
    return {
        "params": params,
        "quotes": quotes,
        "products": oddsrelay_choose_products_from_quotes(quotes),
    }


def oddsrelay_acquire_product(product, params):
    """Token-consuming call. Caller must supply a quote-validated product."""
    if product not in ODDSRELAY_MATCHED_PRODUCTS and product != "raw":
        raise ValueError(f"Unsupported OddsRelay product: {product}")
    query = dict(params or {})
    query.pop("quote", None)
    return oddsrelay_api_get(f"/v2/odds/{product}", query)


def oddsrelay_execute_plan(plan, allowed_products=None):
    """Acquire only products present in a quote-gated plan and explicit allow-list."""
    planned = set(plan.get("products", []))
    allowed = set(allowed_products or [])
    results = {}
    for product in sorted(planned & allowed):
        body, usage = oddsrelay_acquire_product(product, plan.get("params", {}))
        results[product] = {"data": body, "usage": usage}
    return results


def oddsrelay_snapshot_envelope(observed, start_time, end_time, acquisitions):
    """Provider-aware v0.5 envelope; deliberately separate from legacy v0.2 CSV."""
    return {
        "paper_only": True,
        "version": "0.5",
        "schema_version": "0.5",
        "provider": "oddsrelay",
        "observed_at_utc": format_utc_timestamp(observed),
        "observed_at_london": format_london_timestamp(observed),
        "collection_window_start_utc": format_utc_timestamp(start_time),
        "collection_window_end_utc": format_utc_timestamp(end_time),
        "products": acquisitions,
    }


def write_oddsrelay_snapshot(snapshot, data_dir=None):
    directory = Path(data_dir or Path(__file__).parent / "data" / "v0.5")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["observed_at_utc"].replace(":", "").replace("+", "_")
    path = directory / f"oddsrelay_{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def canonical_text(value):
    return " ".join(str(value or "").strip().lower().split())


def canonical_event_key(sport, commence_time, home, away):
    """Cross-provider event identity without relying on provider-specific IDs."""
    return "|".join([
        canonical_text(sport),
        canonical_text(commence_time),
        canonical_text(home),
        canonical_text(away),
    ])


def canonical_market_key(provider, sport, commence_time, home, away, bookmaker, market, outcome, point=None):
    event = canonical_event_key(sport, commence_time, home, away)
    return "|".join([
        event,
        canonical_text(bookmaker),
        canonical_text(market),
        canonical_text(outcome),
        canonical_text(point),
    ])


def dedupe_normalized_observations(rows):
    """Deduplicate provider-overlap while preserving the newest observation."""
    deduped = {}
    for row in rows:
        key = canonical_market_key(
            row.get("provider"), row.get("sport_key") or row.get("sport"),
            row.get("commence_time_utc") or row.get("commence_time"),
            row.get("home"), row.get("away"),
            row.get("bookmaker_key") or row.get("bookmaker"),
            row.get("market_key") or row.get("market"),
            row.get("outcome_name") or row.get("outcome"), row.get("point"),
        )
        current = deduped.get(key)
        if current is None or str(row.get("observed_at_utc", "")) >= str(current.get("observed_at_utc", "")):
            deduped[key] = row
    return list(deduped.values())


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def normalize_oddsrelay_payload(product, payload, observed_at_utc):
    """Conservative adapter: emit rows only when canonical betting fields are explicit."""
    rows = []
    for item in _walk_dicts(payload):
        price = item.get("price_decimal", item.get("decimal_odds", item.get("odds")))
        market = item.get("market_key", item.get("market"))
        outcome = item.get("outcome_name", item.get("outcome", item.get("selection")))
        bookmaker = item.get("bookmaker_key", item.get("bookmaker"))
        commence = item.get("commence_time_utc", item.get("commence_time", item.get("start_time")))
        sport = item.get("sport_key", item.get("sport"))
        home = item.get("home", item.get("home_team"))
        away = item.get("away", item.get("away_team"))
        if None in (price, market, outcome, bookmaker, commence, sport, home, away):
            continue
        try:
            decimal_price = float(price)
        except (TypeError, ValueError):
            continue
        if decimal_price <= 1.0:
            continue
        rows.append({
            "provider": "oddsrelay", "product": product,
            "observed_at_utc": observed_at_utc,
            "sport_key": str(sport), "commence_time_utc": str(commence),
            "home": str(home), "away": str(away),
            "bookmaker_key": str(bookmaker), "market_key": str(market),
            "outcome_name": str(outcome), "point": item.get("point", item.get("line")),
            "price_decimal": decimal_price,
        })
    return rows


def normalize_oddsrelay_acquisitions(acquisitions, observed_at_utc):
    rows = []
    for product, result in acquisitions.items():
        rows.extend(normalize_oddsrelay_payload(product, result.get("data"), observed_at_utc))
    return dedupe_normalized_observations(rows)


def unified_snapshot_envelope(observed, start_time, end_time, the_odds_api_rows, oddsrelay_rows, raw_refs=None):
    """v0.5 canonical observation universe with provider provenance retained per row."""
    api_rows = [dict(row, provider=row.get("provider", "the_odds_api")) for row in the_odds_api_rows]
    relay_rows = [dict(row, provider=row.get("provider", "oddsrelay")) for row in oddsrelay_rows]
    combined = dedupe_normalized_observations(api_rows + relay_rows)
    return {
        "paper_only": True,
        "version": "0.5",
        "schema_version": "0.5",
        "observed_at_utc": format_utc_timestamp(observed),
        "observed_at_london": format_london_timestamp(observed),
        "collection_window_start_utc": format_utc_timestamp(start_time),
        "collection_window_end_utc": format_utc_timestamp(end_time),
        "source_counts": {
            "the_odds_api": len(api_rows),
            "oddsrelay": len(relay_rows),
            "canonical": len(combined),
        },
        "raw_source_refs": raw_refs or {},
        "observations": combined,
    }


def write_unified_snapshot(snapshot, data_dir=None):
    directory = Path(data_dir or Path(__file__).parent / "data" / "v0.5")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["observed_at_utc"].replace(":", "").replace("+", "_")
    path = directory / f"unified_{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path

def get_active_sports():
    sports, quota = api_get("/sports")
    active = [sport for sport in (sports or []) if sport.get("active")]
    return sorted(active, key=lambda sport: (sport.get("title") or "").lower()), quota

def sport_family(sport):
    candidates = [
        normalize_sport_name(sport.get("key")),
        normalize_sport_name(sport.get("group")),
        normalize_sport_name(sport.get("title")),
    ]
    for family, aliases in PAPER_ONLY_SPORT_ALIASES.items():
        normalized_aliases = {normalize_sport_name(alias) for alias in aliases}
        if any(
            candidate == alias or candidate.startswith(f"{alias}_")
            for candidate in candidates
            for alias in normalized_aliases
        ):
            return family
    return "other"

def select_sports_for_budget(sports, budget):
    """Round-robin across sport families so an alphabetical list cannot consume the budget."""
    budget = max(0, int(budget))
    if budget == 0:
        return []
    buckets = {}
    family_order = list(PAPER_ONLY_SPORT_ALIASES) + ["other"]
    for sport in sports or []:
        buckets.setdefault(sport_family(sport), []).append(sport)
    for bucket in buckets.values():
        bucket.sort(key=lambda sport: (sport.get("title") or "").lower())

    selected = []
    while len(selected) < budget:
        added = False
        for family in family_order:
            bucket = buckets.get(family, [])
            if bucket and len(selected) < budget:
                selected.append(bucket.pop(0))
                added = True
        if not added:
            break
    return selected


def default_collection_window(now=None):
    """Collect from observation time until the next 10:00 Europe/London boundary."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    start = now.astimezone(timezone.utc)
    local_now = start.astimezone(LONDON)
    end_local = local_now.replace(hour=10, minute=0, second=0, microsecond=0)
    if local_now >= end_local:
        end_local += timedelta(days=1)
    return start, end_local.astimezone(timezone.utc)


def filter_events_between(events, start, end):
    """Return events whose commence time falls inside explicit aware boundaries."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Collection boundaries must be timezone-aware")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end < start:
        raise ValueError("Collection window end precedes start")

    filtered = []
    for event in events or []:
        commence_time = event.get("commence_time")
        if not commence_time:
            continue
        parsed = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if start <= parsed <= end:
            filtered.append(event)
    return filtered


def filter_upcoming_events(events, now=None, start_hours=0, end_hours=168):
    """Compatibility wrapper for explicit hour-offset tests and callers."""
    if now is None:
        now = datetime.now(timezone.utc)
    start = now + timedelta(hours=start_hours)
    end = now + timedelta(hours=end_hours)
    return filter_events_between(events, start, end)


def get_odds(sport_key, markets=None):
    if markets is None:
        markets = default_market_keys()
    return api_get(
        f"/sports/{urllib.parse.quote(sport_key, safe='')}/odds/",
        {
            "regions": REGIONS,
            "markets": ",".join(markets),
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": "iso",
        },
    )


def compute_market_de_vigged_probabilities(rows):
    grouped = {}

    for row in rows:
        item = dict(row)
        item["raw_implied_probability"] = item.get("raw_implied_probability")
        if item["raw_implied_probability"] is None and item.get("price_decimal"):
            item["raw_implied_probability"] = 1.0 / float(item["price_decimal"])

        market = item.get("market")
        event_id = item.get("event_id")
        bookmaker_key = item.get("bookmaker_key")
        point = item.get("point")

        if market in {"h2h"}:
            key = (event_id, bookmaker_key, market)
        elif market in {"spreads", "totals"} and point is not None:
            key = (event_id, bookmaker_key, market, round(abs(float(point)), 4))
        else:
            key = (event_id, bookmaker_key, market, item.get("outcome"))

        grouped.setdefault(key, []).append(item)

    flattened = []
    for group in grouped.values():
        if len(group) <= 1:
            for item in group:
                item["market_de_vigged_probability"] = 1.0
                flattened.append(item)
            continue

        total_probability = sum(float(item.get("raw_implied_probability", 0.0)) for item in group)
        if total_probability <= 0:
            for item in group:
                item["market_de_vigged_probability"] = 1.0
                flattened.append(item)
            continue

        for item in group:
            item["market_de_vigged_probability"] = (
                float(item.get("raw_implied_probability", 0.0)) / total_probability
            )
            flattened.append(item)

    return flattened


def flatten_events(events, observed_utc, observed_london):
    rows = []

    for event in events or []:
        commence_time_utc = event.get("commence_time")
        commence_time_london = None
        if commence_time_utc:
            commence_time_london = format_london_timestamp(commence_time_utc)

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price_decimal = outcome.get("price")
                    raw_probability = None
                    if price_decimal:
                        raw_probability = 1.0 / float(price_decimal)

                    rows.append(
                        {
                            "observed_at_utc": observed_utc,
                            "observed_at_london": observed_london,
                            "event_id": event.get("id"),
                            "sport_key": event.get("sport_key"),
                            "sport_title": event.get("sport_title"),
                            "commence_time_utc": format_utc_timestamp(commence_time_utc),
                            "commence_time_london": commence_time_london,
                            "home_team": event.get("home_team"),
                            "away_team": event.get("away_team"),
                            "bookmaker_key": bookmaker.get("key"),
                            "bookmaker_title": bookmaker.get("title"),
                            "bookmaker_last_update": bookmaker.get("last_update"),
                            "market": market.get("key"),
                            "outcome": outcome.get("name"),
                            "price_decimal": price_decimal,
                            "point": outcome.get("point"),
                            "raw_implied_probability": raw_probability,
                            "market_de_vigged_probability": 1.0,
                            "schema_version": "0.2",
                        }
                    )

    return rows


def detect_csv_schema(csv_path):
    if not csv_path or not csv_path.exists():
        return None
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
    except OSError:
        return None

    if not header:
        return None

    normalized = [column.strip() for column in header]
    for schema_version, fieldnames in CSV_SCHEMA_VERSIONS.items():
        if normalized == fieldnames:
            return schema_version
    return "unknown"


def resolve_snapshot_path(day, schema_version="0.2"):
    default_path = DATA_DIR / f"odds-{day}.csv"
    if not default_path.exists():
        return default_path

    existing_schema = detect_csv_schema(default_path)
    if existing_schema == schema_version:
        return default_path
    if existing_schema in (None, schema_version):
        return default_path
    return DATA_DIR / f"odds-{day}-v{schema_version}.csv"


def save_snapshot(rows, metadata):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now_london = datetime.now(LONDON)
    day = now_london.strftime("%Y-%m-%d")
    stamp = now_london.strftime("%Y%m%dT%H%M%S%z")
    schema_version = str(metadata.get("schema_version") or metadata.get("version") or "0.2")
    if schema_version not in CSV_SCHEMA_VERSIONS:
        schema_version = "0.2"

    csv_path = resolve_snapshot_path(day, schema_version)
    metadata_path = DATA_DIR / f"run-{stamp}-v{schema_version}.json"

    rows_to_write = []
    for row in rows or []:
        normalized_row = dict(row)
        normalized_row.setdefault("schema_version", schema_version)
        rows_to_write.append({field: normalized_row.get(field) for field in CSV_SCHEMA_VERSIONS[schema_version]})

    file_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_SCHEMA_VERSIONS[schema_version])
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows_to_write)

    safe_metadata = dict(metadata)
    safe_metadata["schema_version"] = schema_version
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(safe_metadata, handle, indent=2)

    return csv_path, metadata_path



def default_oddsrelay_acquisition_products():
    """Broad matched-board baseline; specialist/promotional boards stay opt-in."""
    configured = os.environ.get("ODDSRELAY_PRODUCTS", "standard")
    requested = [p.strip().lower() for p in configured.split(",") if p.strip()]
    valid = set(ODDSRELAY_MATCHED_PRODUCTS) | {"raw"}
    unknown = [p for p in requested if p not in valid]
    if unknown:
        raise ValueError("Unsupported ODDSRELAY_PRODUCTS: " + ", ".join(unknown))
    return requested


def run_oddsrelay_collection(observed, start_time, end_time):
    """Quote-gated live OddsRelay collection; acquisition products are explicit."""
    plan = oddsrelay_build_acquisition_plan(start_time, end_time)
    allowed = default_oddsrelay_acquisition_products()
    acquisitions = oddsrelay_execute_plan(plan, allowed)
    raw_snapshot = oddsrelay_snapshot_envelope(observed, start_time, end_time, acquisitions)
    raw_path = write_oddsrelay_snapshot(raw_snapshot)
    rows = normalize_oddsrelay_acquisitions(acquisitions, format_utc_timestamp(observed))
    return rows, raw_path, plan, acquisitions

def main():
    observed = datetime.now(timezone.utc)
    observed_utc = observed.isoformat()
    observed_london = observed.astimezone(LONDON).isoformat()
    start_time_utc, end_time_utc = default_collection_window(observed)

    sports, sports_quota = get_active_sports()
    sports_discovered = sports
    sports_queried = []
    chargeable_requests_made = 0
    budget_limit = default_request_budget()
    events_returned = 0
    rows_recorded = 0
    quota_history = []
    all_rows = []

    print(f"Sports discovered: {len(sports_discovered)}")
    print(f"Sports queried: 0")
    print(f"Chargeable requests made: 0")
    print(f"Events returned: 0")
    print(f"Rows recorded: 0")
    print(f"Credits used: 0")
    print(f"Credits remaining: {sports_quota.get('requests_remaining', 'n/a')}")

    if not sports_discovered:
        print("No active paper-only sports available for this collection window.")
        return

    sports_selected = select_sports_for_budget(sports_discovered, budget_limit)
    for sport in sports_selected:

        sport_key = sport.get("key")
        if not sport_key:
            continue

        requested_markets = default_market_keys()
        events, odds_quota = get_odds(sport_key, requested_markets)
        chargeable_requests_made += 1
        quota_history.append(
            {
                "sport_key": sport_key,
                "sport_title": sport.get("title"),
                "requested_markets": requested_markets,
                "quota_headers": odds_quota,
            }
        )
        sports_queried.append(sport)

        filtered_events = filter_events_between(events, start_time_utc, end_time_utc)
        events_returned += len(filtered_events)

        rows = flatten_events(filtered_events, observed_utc, observed_london)
        if rows:
            rows = compute_market_de_vigged_probabilities(rows)
            all_rows.extend(rows)
            rows_recorded += len(rows)

        print(
            f"Querying {sport.get('title')} ({sport_key}) — "
            f"market(s): {', '.join(requested_markets)}"
        )

    metadata = {
        "paper_only": True,
        "version": "0.4",
        "schema_version": "0.2",
        "observed_at_utc": observed_utc,
        "observed_at_london": observed_london,
        "collection_window_start_utc": format_utc_timestamp(start_time_utc),
        "collection_window_end_utc": format_utc_timestamp(end_time_utc),
        "collection_window_start_london": format_london_timestamp(start_time_utc),
        "collection_window_end_london": format_london_timestamp(end_time_utc),
        "regions": REGIONS,
        "markets": default_market_keys(),
        "odds_format": ODDS_FORMAT,
        "request_budget_limit": budget_limit,
        "chargeable_requests_made": chargeable_requests_made,
        "sports_discovered": len(sports_discovered),
        "sports_queried": len(sports_queried),
        "events_returned": events_returned,
        "rows_recorded": rows_recorded,
        "quota_history": quota_history,
    }

    if all_rows:
        csv_path, metadata_path = save_snapshot(all_rows, metadata)
    else:
        csv_path = DATA_DIR / f"odds-{datetime.now(LONDON).strftime('%Y-%m-%d')}.csv"
        metadata_path = DATA_DIR / f"run-{datetime.now(LONDON).strftime('%Y%m%dT%H%M%S%z')}.json"

    print(f"Sports discovered: {len(sports_discovered)}")
    print(f"Sports queried: {len(sports_queried)}")
    print(f"Chargeable requests made: {chargeable_requests_made}")
    print(f"Events returned: {events_returned}")
    print(f"Rows recorded: {rows_recorded}")

    credits_used = None
    credits_remaining = None
    if quota_history:
        last_quota = quota_history[-1]["quota_headers"]
        credits_used = last_quota.get("requests_used")
        credits_remaining = last_quota.get("requests_remaining")

    print(f"Credits used: {credits_used if credits_used is not None else 'n/a'}")
    print(f"Credits remaining: {credits_remaining if credits_remaining is not None else 'n/a'}")
    print(f"CSV: {csv_path}")
    print(f"Metadata: {metadata_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Paper Lab logger failed: {exc}", file=sys.stderr)
        sys.exit(1)

ODDSRELAY_MATCHED_PRODUCTS = ("standard", "2up", "dutching", "each-way", "extra-place", "bog")


def oddsrelay_quote(product, params=None):
    if product not in ODDSRELAY_MATCHED_PRODUCTS and product != "raw":
        raise ValueError(f"Unsupported OddsRelay product: {product}")
    query = dict(params or {})
    query["quote"] = "true"
    return oddsrelay_api_get(f"/v2/odds/{product}", query)


def oddsrelay_quote_catalogue(params=None):
    quotes = {}
    for product in ODDSRELAY_MATCHED_PRODUCTS + ("raw",):
        try:
            body, usage = oddsrelay_quote(product, params)
            quotes[product] = {"quote": body, "usage": usage}
        except Exception as exc:
            quotes[product] = {"error": type(exc).__name__, "detail": str(exc)[:300]}
    return quotes
