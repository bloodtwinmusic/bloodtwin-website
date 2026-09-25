# £10 Paper Lab Odds Logger v0.2

Paper-only research for the blood.twin £10 Paper Lab.

This logger records prospective bookmaker odds only. It never places bets, accesses bookmaker accounts, or initiates any real-money action.

## Purpose

The logger captures pre-event market snapshots so the experiment can analyse:

- price movement
- de-vigged market probabilities
- estimated edge
- entry-price timing
- closing-line value (CLV)
- model calibration

Historical observations are append-only and preserved rather than reconstructed after results are known.

## v0.2 changes

- Uses The Odds API v4 with the `ODDS_API_KEY` environment variable only.
- Uses Europe/London timestamps for human-readable observation and event times while preserving UTC timestamps.
- Uses the free `/sports` endpoint to discover active sports.
- Restricts collection to a curated allowlist of paper-lab relevant sports: soccer, tennis, basketball, american football, baseball, and ice hockey.
- Defaults to a tightly controlled H2H market configuration only. Other supported markets are configured explicitly via the market list and remain opt-in.
- Enforces an API request budget of 10 chargeable odds requests by default and stops before exceeding the limit.
- Keeps the UK region and decimal odds format for this research stream.
- Records quota headers after each chargeable odds request in the run metadata.
- Derives raw implied probabilities as $1 / \text{decimal odds}$ and de-vigging uses same-bookmaker, same-market groupings only.
- Prints a clear run summary showing sports discovered, sports queried, chargeable requests, events returned, rows recorded, credits used, and credits remaining.

## Quota and collection controls

The logger is intentionally conservative because The Odds API credit budget is limited.

- Default request budget: 10 chargeable odds requests per run.
- No automatic querying of every active sport.
- Sports are filtered through the allowlist before any odds requests are made.
- The collection window is configurable via environment variables:
  - `PAPER_LAB_START_HOURS`: number of hours ahead to start collection from now
  - `PAPER_LAB_END_HOURS`: number of hours ahead to stop collection from now
  - `ODDS_REQUEST_BUDGET`: hard cap for chargeable odds requests in one run
  - `ODDS_MARKETS`: comma-separated market list, defaulting to `h2h`

## Snapshot fields

Each recorded row includes:

- observed_at_utc
- observed_at_london
- event_id
- sport_key
- sport_title
- commence_time_utc
- commence_time_london
- home_team
- away_team
- bookmaker_key
- bookmaker_title
- bookmaker_last_update
- market
- outcome
- price_decimal
- point
- raw_implied_probability
- market_de_vigged_probability

The CSV is append-only. The logger never overwrites historical observations or reconstructs them from old snapshots.

## Safety note

This system is paper-only and intentionally never performs real-money activity. It does not access bookmaker accounts, betting interfaces, or payment systems.
