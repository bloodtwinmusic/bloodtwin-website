import csv
import gzip
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_URL = "https://api.the-odds-api.com/v4"
REGIONS = "uk"
ODDS_FORMAT = "decimal"
DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
V0_5_DATA_DIR = DATA_DIR / "v0.5"
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
    configured = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals")
    markets = [item.strip() for item in configured.split(",") if item.strip()]
    return markets or ["h2h", "spreads", "totals"]


def default_request_budget():
    raw_value = os.environ.get("ODDS_REQUEST_BUDGET", "8")
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 8


def default_credit_budget():
    """Maximum The Odds API credits for one collection, not HTTP requests."""
    return max(0, env_int("ODDS_CREDIT_BUDGET", 10))


def default_credit_reserve():
    """Credits preserved as a hard floor for diagnostics and future decisions."""
    return max(0, env_int("ODDS_CREDIT_RESERVE", 50))


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


def oddsrelay_quote_token_estimate(result):
    """Extract the acquisition estimate from a zero-token quote response."""
    quote = result.get("quote") if isinstance(result, dict) else None
    preferred_keys = {"tokens", "token_cost", "tokens_cost", "estimated_tokens", "cost"}

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if normalize_sport_name(key) in preferred_keys:
                    try:
                        return max(0, int(float(child)))
                    except (TypeError, ValueError):
                        pass
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    return walk(quote)


def default_oddsrelay_token_budget():
    return max(0, env_int("ODDSRELAY_TOKEN_BUDGET", 10000))


def oddsrelay_select_products_from_quotes(quotes, requested_products, token_budget=None):
    """Use explicit usefulness policy plus quote estimates; unknown costs are not purchased."""
    remaining = default_oddsrelay_token_budget() if token_budget is None else max(0, int(token_budget))
    provider_remaining = []
    for result in quotes.values():
        try:
            provider_remaining.append(int(result.get("usage", {}).get("tokens_remaining")))
        except (AttributeError, TypeError, ValueError):
            pass
    if provider_remaining:
        remaining = min(remaining, min(provider_remaining))
    selected = []
    estimates = {}
    for product in requested_products:
        result = quotes.get(product, {})
        if "error" in result or "quote" not in result:
            continue
        estimate = oddsrelay_quote_token_estimate(result)
        estimates[product] = estimate
        if estimate is None or estimate > remaining:
            continue
        selected.append(product)
        remaining -= estimate
    return selected, estimates


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
    planned = set(plan.get("selected_products", plan.get("products", [])))
    allowed = list(allowed_products or [])
    results = {}
    for product in allowed:
        if product not in planned:
            continue
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
    """Write raw evidence as gzip outside ordinary Git history."""
    directory = Path(data_dir or RAW_DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["observed_at_utc"].replace(":", "").replace("+", "_")
    path = directory / f"oddsrelay_{stamp}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(snapshot, handle, separators=(",", ":"), sort_keys=True)
    return path


def canonical_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


BOOKMAKER_ALIASES = {
    "betano_uk": "betano",
    "betfair_ex_uk": "betfair_exchange",
    "betfair_sb_uk": "betfair_sportsbook",
    "betfred_uk": "betfred",
    "ladbrokes_uk": "ladbrokes",
    "livescorebet": "livescore_bet",
    "paddypower": "paddy_power",
    "skybet": "sky_bet",
    "sport888": "888sport",
    "unibet_uk": "unibet",
    "virginbet": "virgin_bet",
    "williamhill": "william_hill",
}


PRIMARY_MARKETS = {"h2h", "spreads", "totals"}


PRIMARY_MARKET_BY_FAMILY = {
    "american_football": {"spreads": "handicap_points", "totals": "over_under_points"},
    "baseball": {"spreads": "handicap_runs", "totals": "over_under_runs"},
    "basketball": {"spreads": "handicap_points", "totals": "over_under_points"},
    "cricket": {"spreads": "handicap_runs", "totals": "over_under_runs"},
    "ice_hockey": {"spreads": "handicap_goals", "totals": "totals"},
    "soccer": {"spreads": "handicap_goals", "totals": "totals"},
    "tennis": {"spreads": "handicap_games", "totals": "over_under_games"},
}


def canonical_bookmaker_key(value):
    key = normalize_sport_name(value)
    return BOOKMAKER_ALIASES.get(key, key)


def canonical_sport_family(value):
    key = normalize_sport_name(value)
    if key.startswith("americanfootball"):
        key = "american_football" + key[len("americanfootball"):]
    for family in (
        "american_football", "aussie_rules", "baseball", "basketball", "boxing",
        "cricket", "darts", "esports", "handball", "horse_racing", "ice_hockey",
        "mma", "rugby_league", "rugby_union", "soccer", "tennis", "volleyball",
    ):
        if key == family or key.startswith(f"{family}_"):
            return family
    return key.split("_", 1)[0] if key else "unknown"


def canonical_sport_key(value):
    key = normalize_sport_name(value)
    if key.startswith("americanfootball"):
        key = "american_football" + key[len("americanfootball"):]
    return key


def participant_tokens(value):
    raw = str(value or "").strip()
    if "," in raw:
        last, rest = raw.split(",", 1)
        raw = f"{rest} {last}"
    text = canonical_text(raw)
    replacements = {"saint": "st", "university": "", "women": "", "womens": ""}
    tokens = [replacements.get(token, token) for token in text.split()]
    return [token for token in tokens if token and token not in {"fc", "afc", "cf", "bc"}]


def participant_similarity(left, right):
    left_tokens = participant_tokens(left)
    right_tokens = participant_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    left_text = " ".join(left_tokens)
    right_text = " ".join(right_tokens)
    if left_text == right_text:
        return 1.0
    intersection = len(set(left_tokens) & set(right_tokens))
    token_score = (2.0 * intersection) / (len(set(left_tokens)) + len(set(right_tokens)))
    sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
    initial_score = 0.0
    if left_tokens[-1] == right_tokens[-1] and left_tokens[0][0] == right_tokens[0][0]:
        initial_score = 0.9
    return max(token_score, sequence_score, initial_score)


def infer_outcome_role(name, home, away):
    normalized = canonical_text(name)
    if normalized.startswith("over"):
        return "over"
    if normalized.startswith("under"):
        return "under"
    if normalized in {"draw", "tie", "x"}:
        return "draw"
    home_score = participant_similarity(name, home)
    away_score = participant_similarity(name, away)
    if max(home_score, away_score) >= 0.72:
        return "home" if home_score >= away_score else "away"
    return normalized or "other"


def extract_point(value, outcome_name=None):
    if value not in (None, ""):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    match = re.search(r"(?<![A-Za-z])(-?\d+(?:\.\d+)?)", str(outcome_name or ""))
    return float(match.group(1)) if match else None


def canonical_market_descriptor(source_market, sport_key):
    source = normalize_sport_name(source_market)
    if source.endswith("_lay"):
        source = source[:-4]
    if source == "h2h":
        return "h2h", "result"
    family = canonical_sport_family(sport_key)
    family_markets = PRIMARY_MARKET_BY_FAMILY.get(family, {})
    if source == family_markets.get("spreads"):
        return "spreads", source.replace("handicap_", "")
    if source == family_markets.get("totals"):
        metric = source.replace("over_under_", "")
        return "totals", "score" if metric == "totals" else metric
    if source in {"spreads", "totals"}:
        return source, "score"
    return None, None


def canonical_event_key(sport, commence_time, home, away):
    """Provider-neutral event identity for already-reconciled participant names."""
    return "|".join([
        canonical_sport_family(sport),
        format_utc_timestamp(commence_time) if commence_time else "",
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


def normalized_row_identity(row):
    event = row.get("canonical_event_id") or canonical_event_key(
        row.get("sport_key"), row.get("commence_time_utc"), row.get("home"), row.get("away")
    )
    outcome = row.get("outcome_role") or row.get("outcome_name") or row.get("outcome")
    point = row.get("point")
    if point is not None:
        point = round(float(point), 4)
    return (
        event,
        canonical_bookmaker_key(row.get("bookmaker_key") or row.get("bookmaker")),
        canonical_text(row.get("market_key") or row.get("market")),
        canonical_text(row.get("market_metric")),
        canonical_text(outcome),
        point,
        canonical_text(row.get("price_side") or "back"),
    )


def _freshness_key(row):
    return (
        str(row.get("bookmaker_last_update") or ""),
        str(row.get("observed_at_utc") or ""),
        1 if row.get("provider") == "oddsrelay" else 0,
    )


def dedupe_normalized_observations(rows):
    """Deduplicate canonical quote overlap while preserving merged provenance."""
    deduped = {}
    for row in rows:
        row = dict(row)
        key = normalized_row_identity(row)
        current = deduped.get(key)
        providers = set(row.get("source_providers") or [row.get("provider")])
        if current is None:
            row["source_providers"] = sorted(item for item in providers if item)
            row["duplicate_count"] = int(row.get("duplicate_count") or 1)
            deduped[key] = row
            continue
        providers.update(current.get("source_providers") or [current.get("provider")])
        duplicate_count = int(current.get("duplicate_count") or 1) + int(row.get("duplicate_count") or 1)
        winner = row if _freshness_key(row) >= _freshness_key(current) else current
        winner = dict(winner)
        winner["source_providers"] = sorted(item for item in providers if item)
        winner["duplicate_count"] = duplicate_count
        deduped[key] = winner
    return list(deduped.values())


def normalize_oddsrelay_payload(product, payload, observed_at_utc, allowed_markets=None):
    """Normalize OddsRelay's event/market/outcome/back-or-lay hierarchy."""
    allowed = set(allowed_markets or PRIMARY_MARKETS)
    body = payload or {}
    events = body.get("data", []) if isinstance(body, dict) else body
    meta = body.get("meta", {}) if isinstance(body, dict) else {}
    last_seen = meta.get("last_seen", {}) if isinstance(meta, dict) else {}
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        source_sport = event.get("sport_key")
        family = canonical_sport_family(source_sport)
        commence = event.get("commence_time")
        home = event.get("home_team")
        away = event.get("away_team")
        if None in (source_sport, commence, home, away):
            continue
        for market in event.get("markets", []):
            source_market = market.get("key")
            market_key, metric = canonical_market_descriptor(source_market, source_sport)
            if market_key not in allowed:
                continue
            for outcome in market.get("outcomes", []):
                outcome_name = outcome.get("name")
                outcome_role = infer_outcome_role(outcome_name, home, away)
                point = extract_point(outcome.get("point"), outcome_name)
                for price_side, quote_key, bookmaker_field in (
                    ("back", "back", "bookmaker"), ("lay", "lay", "exchange")
                ):
                    for quote in outcome.get(quote_key, []) or []:
                        bookmaker = quote.get(bookmaker_field)
                        try:
                            decimal_price = float(quote.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if not bookmaker or decimal_price <= 1.0:
                            continue
                        bookmaker_key = canonical_bookmaker_key(bookmaker)
                        bookmaker_updates = last_seen.get(bookmaker, {})
                        bookmaker_update = bookmaker_updates.get(family) if isinstance(bookmaker_updates, dict) else None
                        rows.append({
                            "schema_version": "0.5",
                            "provider": "oddsrelay",
                            "product": product,
                            "provider_event_id": event.get("event_id"),
                            "observed_at_utc": format_utc_timestamp(observed_at_utc),
                            "sport_key": canonical_sport_key(source_sport),
                            "source_sport_key": source_sport,
                            "sport_title": event.get("sport_title"),
                            "sport_family": family,
                            "commence_time_utc": format_utc_timestamp(commence),
                            "home": str(home),
                            "away": str(away),
                            "bookmaker_key": bookmaker_key,
                            "bookmaker_title": bookmaker,
                            "bookmaker_last_update": bookmaker_update,
                            "market_key": market_key,
                            "source_market_key": source_market,
                            "market_metric": metric,
                            "outcome_name": str(outcome_name),
                            "source_outcome_name": str(outcome_name),
                            "outcome_role": outcome_role,
                            "price_side": price_side,
                            "point": point,
                            "price_decimal": decimal_price,
                            "raw_implied_probability": 1.0 / decimal_price,
                            "market_de_vigged_probability": None,
                            "available_amount": quote.get("available"),
                            "source_link": quote.get("link"),
                        })
    return dedupe_normalized_observations(rows)


def normalize_oddsrelay_acquisitions(acquisitions, observed_at_utc):
    rows = []
    for product, result in acquisitions.items():
        rows.extend(normalize_oddsrelay_payload(product, result.get("data"), observed_at_utc))
    return dedupe_normalized_observations(rows)


def normalize_the_odds_api_rows(rows):
    normalized = []
    for source in rows or []:
        source_market = source.get("market")
        price_side = "lay" if str(source_market or "").endswith("_lay") else "back"
        market_key, metric = canonical_market_descriptor(source_market, source.get("sport_key"))
        if market_key not in PRIMARY_MARKETS:
            continue
        try:
            price = float(source.get("price_decimal"))
        except (TypeError, ValueError):
            continue
        if price <= 1.0:
            continue
        home = source.get("home_team")
        away = source.get("away_team")
        outcome_name = source.get("outcome")
        normalized.append({
            "schema_version": "0.5",
            "provider": "the_odds_api",
            "product": "odds",
            "provider_event_id": source.get("event_id"),
            "observed_at_utc": format_utc_timestamp(source.get("observed_at_utc")),
            "sport_key": canonical_sport_key(source.get("sport_key")),
            "source_sport_key": source.get("sport_key"),
            "sport_title": source.get("sport_title"),
            "sport_family": canonical_sport_family(source.get("sport_key")),
            "commence_time_utc": format_utc_timestamp(source.get("commence_time_utc")),
            "home": str(home),
            "away": str(away),
            "bookmaker_key": canonical_bookmaker_key(source.get("bookmaker_key")),
            "bookmaker_title": source.get("bookmaker_title"),
            "bookmaker_last_update": source.get("bookmaker_last_update"),
            "market_key": market_key,
            "source_market_key": source_market,
            "market_metric": metric,
            "outcome_name": str(outcome_name),
            "source_outcome_name": str(outcome_name),
            "outcome_role": infer_outcome_role(outcome_name, home, away),
            "price_side": price_side,
            "point": extract_point(source.get("point"), outcome_name),
            "price_decimal": price,
            "raw_implied_probability": source.get("raw_implied_probability") or 1.0 / price,
            "market_de_vigged_probability": (
                source.get("market_de_vigged_probability") if price_side == "back" else None
            ),
            "available_amount": None,
            "source_link": None,
        })
    return normalized


def _event_descriptor_key(row):
    provider_id = row.get("provider_event_id")
    if provider_id:
        return row.get("provider"), str(provider_id)
    return (
        row.get("provider"), row.get("sport_key"), row.get("commence_time_utc"),
        row.get("home"), row.get("away"),
    )


def reconcile_canonical_events(rows):
    """Cluster provider event IDs by time, sport family and participant similarity."""
    descriptors = {}
    for row in rows:
        descriptors.setdefault(_event_descriptor_key(row), row)
    ordered = sorted(
        descriptors.items(),
        key=lambda item: (
            item[1].get("sport_family") or "",
            item[1].get("commence_time_utc") or "",
            0 if item[1].get("provider") == "the_odds_api" else 1,
            str(item[0]),
        ),
    )
    clusters_by_slot = {}
    assignments = {}
    for descriptor_key, row in ordered:
        slot = (row.get("sport_family"), row.get("commence_time_utc"))
        clusters = clusters_by_slot.setdefault(slot, [])
        best = None
        for cluster in clusters:
            normal = min(
                participant_similarity(row.get("home"), cluster["home"]),
                participant_similarity(row.get("away"), cluster["away"]),
            )
            swapped = min(
                participant_similarity(row.get("home"), cluster["away"]),
                participant_similarity(row.get("away"), cluster["home"]),
            )
            score = max(normal, swapped)
            if score >= 0.72 and (best is None or score > best[0]):
                best = (score, cluster, swapped > normal)
        if best is None:
            seed = canonical_event_key(row.get("sport_family"), row.get("commence_time_utc"), row.get("home"), row.get("away"))
            cluster = {
                "id": "evt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
                "home": row.get("home"),
                "away": row.get("away"),
                "sport_key": row.get("sport_key"),
            }
            clusters.append(cluster)
            swapped = False
        else:
            _, cluster, swapped = best
        assignments[descriptor_key] = (cluster, swapped)

    reconciled = []
    for source in rows:
        row = dict(source)
        cluster, swapped = assignments[_event_descriptor_key(row)]
        row["provider_home"] = row.get("home")
        row["provider_away"] = row.get("away")
        row["canonical_event_id"] = cluster["id"]
        row["home"] = cluster["home"]
        row["away"] = cluster["away"]
        row["sport_key"] = cluster["sport_key"]
        if swapped and row.get("outcome_role") in {"home", "away"}:
            row["outcome_role"] = "away" if row["outcome_role"] == "home" else "home"
        reconciled.append(row)
    return reconciled


def compute_canonical_de_vigged_probabilities(rows):
    grouped = {}
    for row in rows:
        if row.get("price_side") != "back":
            continue
        point = row.get("point")
        line = round(abs(float(point)), 4) if point is not None else None
        key = (
            row.get("canonical_event_id"), row.get("bookmaker_key"), row.get("market_key"),
            row.get("market_metric"), line,
        )
        grouped.setdefault(key, []).append(row)
    for group in grouped.values():
        total = sum(float(row.get("raw_implied_probability") or 0.0) for row in group)
        complete = len({row.get("outcome_role") for row in group}) >= 2
        for row in group:
            row["market_de_vigged_probability"] = (
                float(row.get("raw_implied_probability") or 0.0) / total
                if total > 0 and complete else None
            )
    return rows


CANONICAL_REQUIRED_FIELDS = {
    "schema_version", "provider", "provider_event_id", "observed_at_utc", "sport_key",
    "sport_family", "commence_time_utc", "home", "away", "bookmaker_key",
    "market_key", "outcome_role", "price_side", "price_decimal",
}


def validate_canonical_rows(rows):
    errors = []
    for index, row in enumerate(rows):
        missing = sorted(field for field in CANONICAL_REQUIRED_FIELDS if row.get(field) in (None, ""))
        if missing:
            errors.append(f"row {index}: missing {', '.join(missing)}")
        if row.get("market_key") not in PRIMARY_MARKETS:
            errors.append(f"row {index}: unsupported market {row.get('market_key')}")
        if row.get("price_side") not in {"back", "lay"}:
            errors.append(f"row {index}: unsupported price side {row.get('price_side')}")
        try:
            if float(row.get("price_decimal")) <= 1.0:
                errors.append(f"row {index}: invalid decimal price")
        except (TypeError, ValueError):
            errors.append(f"row {index}: invalid decimal price")
    return errors


def unified_snapshot_envelope(observed, start_time, end_time, the_odds_api_rows, oddsrelay_rows, raw_refs=None):
    """v0.5 canonical observation universe with provider provenance retained per row."""
    api_rows = normalize_the_odds_api_rows(the_odds_api_rows)
    relay_rows = [dict(row, provider=row.get("provider", "oddsrelay")) for row in oddsrelay_rows]
    reconciled = reconcile_canonical_events(api_rows + relay_rows)
    validation_errors = validate_canonical_rows(reconciled)
    if validation_errors:
        raise ValueError("Canonical v0.5 validation failed: " + "; ".join(validation_errors[:10]))
    combined = dedupe_normalized_observations(reconciled)
    compute_canonical_de_vigged_probabilities(combined)
    cross_provider_rows = sum(1 for row in combined if len(row.get("source_providers", [])) > 1)
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
            "duplicates_collapsed": len(api_rows) + len(relay_rows) - len(combined),
            "cross_provider_rows": cross_provider_rows,
        },
        "raw_source_refs": raw_refs or {},
        "observations": combined,
    }


def write_unified_snapshot(snapshot, data_dir=None):
    """Write the full canonical board beside raw evidence for release-asset archival."""
    directory = Path(data_dir or RAW_DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["observed_at_utc"].replace(":", "").replace("+", "_")
    path = directory / f"unified_{stamp}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(snapshot, handle, separators=(",", ":"), sort_keys=True)
    return path


def file_integrity(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    filename = Path(path).name
    return {
        "filename": filename,
        "bytes": Path(path).stat().st_size,
        "sha256": digest.hexdigest(),
        "storage_class": "durable_release" if filename.startswith("unified_") else "workflow_artifact_30_days",
    }


def write_collection_manifest(observed, raw_paths, summary, data_dir=None):
    directory = Path(data_dir or V0_5_DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = format_utc_timestamp(observed).replace(":", "").replace("+", "_")
    manifest = {
        "paper_only": True,
        "schema_version": "0.5",
        "observed_at_utc": format_utc_timestamp(observed),
        "archive_release": f"paper-lab-archive-{observed.astimezone(timezone.utc):%Y-%m}",
        "files": [file_integrity(path) for path in raw_paths if path],
        "summary": summary,
    }
    path = directory / f"manifest_{stamp}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
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

def collection_cycle_number(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(LONDON)
    return now.date().toordinal() * 2 + (1 if now.hour >= 13 else 0)


def select_sports_for_budget(sports, budget, cycle=None):
    """Family-diversified rotating sample; repeated cycles traverse the live catalogue."""
    budget = max(0, int(budget))
    if budget == 0:
        return []
    cycle = collection_cycle_number() if cycle is None else int(cycle)
    buckets = {}
    family_order = list(PAPER_ONLY_SPORT_ALIASES) + ["other"]
    for sport in sports or []:
        buckets.setdefault(sport_family(sport), []).append(sport)
    for bucket in buckets.values():
        bucket.sort(key=lambda sport: (sport.get("title") or "").lower())
        if bucket:
            offset = cycle % len(bucket)
            bucket[:] = bucket[offset:] + bucket[:offset]

    if family_order:
        family_offset = cycle % len(family_order)
        family_order = family_order[family_offset:] + family_order[:family_offset]

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


def plan_the_odds_api_requests(sports, credit_budget, max_requests=None, markets=None, cycle=None):
    """Spend a bounded credit budget on broad H2H plus rotating deep market coverage."""
    credit_budget = max(0, int(credit_budget))
    max_requests = default_request_budget() if max_requests is None else max(0, int(max_requests))
    requested_markets = list(markets or default_market_keys())
    if "h2h" not in requested_markets:
        requested_markets.insert(0, "h2h")
    requested_markets = list(dict.fromkeys(requested_markets))
    if credit_budget == 0 or max_requests == 0:
        return []

    deep_markets = requested_markets
    upgrade_cost = max(0, len(deep_markets) - 1)
    deep_count = 1 if upgrade_cost and credit_budget >= len(deep_markets) else 0
    sport_count = min(max_requests, credit_budget - (deep_count * upgrade_cost))
    selected = select_sports_for_budget(sports, sport_count, cycle=cycle)
    plan = []
    for index, sport in enumerate(selected):
        sport_markets = deep_markets if index < deep_count else ["h2h"]
        plan.append({
            "sport": sport,
            "markets": sport_markets,
            "estimated_credits": len(sport_markets),
        })
    return plan


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
    selected, estimates = oddsrelay_select_products_from_quotes(plan.get("quotes", {}), allowed)
    plan["selected_products"] = selected
    plan["token_estimates"] = estimates
    plan["token_budget"] = default_oddsrelay_token_budget()
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
    request_budget_limit = default_request_budget()
    configured_credit_budget = default_credit_budget()
    credit_reserve = default_credit_reserve()
    try:
        credits_before = int(sports_quota.get("requests_remaining"))
    except (TypeError, ValueError):
        credits_before = None
    available_above_reserve = (
        max(0, credits_before - credit_reserve) if credits_before is not None else configured_credit_budget
    )
    credit_budget = min(configured_credit_budget, available_above_reserve)
    events_returned = 0
    rows_recorded = 0
    credits_spent = 0
    quota_history = []
    all_rows = []

    print(f"Sports discovered: {len(sports_discovered)}")
    request_plan = plan_the_odds_api_requests(
        sports_discovered,
        credit_budget,
        max_requests=request_budget_limit,
        markets=default_market_keys(),
        cycle=collection_cycle_number(observed),
    )
    if not request_plan:
        print("The Odds API paid collection skipped: no sports or credit budget above reserve.")

    for planned_request in request_plan:
        sport = planned_request["sport"]
        sport_key = sport.get("key")
        if not sport_key:
            continue

        requested_markets = planned_request["markets"]
        events, odds_quota = get_odds(sport_key, requested_markets)
        chargeable_requests_made += 1
        try:
            credits_spent += int(odds_quota.get("requests_last") or planned_request["estimated_credits"])
        except (TypeError, ValueError):
            credits_spent += planned_request["estimated_credits"]
        quota_history.append(
            {
                "sport_key": sport_key,
                "sport_title": sport.get("title"),
                "requested_markets": requested_markets,
                "estimated_credits": planned_request["estimated_credits"],
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

    oddsrelay_rows = []
    oddsrelay_raw_path = None
    oddsrelay_plan = None
    oddsrelay_acquisitions = {}
    unified = None
    unified_path = None
    if oddsrelay_key_present():
        oddsrelay_rows, oddsrelay_raw_path, oddsrelay_plan, oddsrelay_acquisitions = run_oddsrelay_collection(
            observed, start_time_utc, end_time_utc
        )
        unified = unified_snapshot_envelope(
            observed, start_time_utc, end_time_utc, all_rows, oddsrelay_rows,
            {"oddsrelay": oddsrelay_raw_path.name} if oddsrelay_raw_path else {},
        )
        unified_path = write_unified_snapshot(unified)
        print(f"OddsRelay normalized rows: {len(oddsrelay_rows)}")
        print(f"OddsRelay selected products: {', '.join(oddsrelay_plan.get('selected_products', [])) or 'none'}")
        print(f"OddsRelay acquired products: {', '.join(sorted(oddsrelay_acquisitions)) or 'none'}")
        for product, result in sorted(oddsrelay_acquisitions.items()):
            usage = result.get("usage", {})
            print(f"OddsRelay {product} tokens cost: {usage.get('tokens_cost') or 'unspecified'}")
        print(f"Unified v0.5 rows: {len(unified['observations'])}")
        print(f"Unified snapshot: {unified_path}")
    else:
        unified = unified_snapshot_envelope(
            observed, start_time_utc, end_time_utc, all_rows, [], {}
        )
        unified_path = write_unified_snapshot(unified)

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
        "request_budget_limit": request_budget_limit,
        "credit_budget_limit": configured_credit_budget,
        "credit_reserve": credit_reserve,
        "credits_before": credits_before,
        "credits_spent": credits_spent,
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

    credits_remaining = None
    if quota_history:
        last_quota = quota_history[-1]["quota_headers"]
        credits_remaining = last_quota.get("requests_remaining")
    elif credits_before is not None:
        credits_remaining = credits_before

    manifest_summary = {
        "the_odds_api": {
            "sports_discovered": len(sports_discovered),
            "sports_queried": len(sports_queried),
            "requests": chargeable_requests_made,
            "credits_spent": credits_spent,
            "credits_remaining": credits_remaining,
            "rows": len(normalize_the_odds_api_rows(all_rows)),
        },
        "oddsrelay": {
            "selected_products": oddsrelay_plan.get("selected_products", []) if oddsrelay_plan else [],
            "token_estimates": oddsrelay_plan.get("token_estimates", {}) if oddsrelay_plan else {},
            "products_acquired": sorted(oddsrelay_acquisitions),
            "rows": len(oddsrelay_rows),
        },
        "unified": unified.get("source_counts", {}) if unified else {},
    }
    manifest_path = write_collection_manifest(
        observed,
        [oddsrelay_raw_path, unified_path],
        manifest_summary,
    )

    print(f"Credits used this collection: {credits_spent}")
    print(f"Credits remaining: {credits_remaining if credits_remaining is not None else 'n/a'}")
    print(f"CSV: {csv_path}")
    print(f"Metadata: {metadata_path}")
    print(f"Evidence manifest: {manifest_path}")


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


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Paper Lab logger failed: {exc}", file=sys.stderr)
        sys.exit(1)
