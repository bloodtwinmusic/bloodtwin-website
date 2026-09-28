# £10 Paper Lab dual-provider collector v0.5

Paper-only market measurement for the blood.twin £10 Paper Lab. The collector records pre-event prices; it never signs in to bookmakers, places bets or initiates real-money activity.

## Providers and canonical data

- **The Odds API** supplies a rotating, worldwide, credit-bounded sample.
- **OddsRelay** supplies a broad UK Standard board after a free quote confirms that the acquisition fits the token budget.
- Both providers normalize into canonical v0.5 rows before union and deduplication. The Odds API's legacy v0.2 CSV remains an append-only source record.
- Canonical markets are `h2h`, `spreads` and `totals`. Source-specific market names, bookmaker names, event IDs, outcome names and price side remain attached for provenance.
- Event reconciliation uses start time, sport family and participant similarity. Bookmaker aliases and outcome roles are canonicalized before duplicate quotes collapse. Every surviving row records `source_providers` and `duplicate_count`.
- De-vigging is calculated only for complete same-event, same-bookmaker, same-market, same-line back-price groups. Lay prices and incomplete groups remain `null` rather than receiving a false probability.

## Proven live schema

Run #81 (`36260469588`) returned the OddsRelay Standard hierarchy:

`event -> markets[] -> outcomes[] -> back[] / lay[]`

The first live specimen is preserved unchanged at `data/v0.5/oddsrelay_2026-09-26T175109.140035_0000.json`. A compact fixture under `fixtures/` protects this schema in zero-spend regression tests. `data/v0.5/run81-normalization-audit.json` records the specimen hash and real-data reconciliation counts.

## Allowance controls

The Odds API documents a cost of one credit per returned market per region. The default 10-credit cycle therefore queries eight rotating sports: one for `h2h,spreads,totals` and seven for `h2h`. Repeated cycles rotate through each live sport-family bucket instead of repeatedly selecting the same alphabetical leagues.

- `ODDS_CREDIT_BUDGET` — default `10` credits per collection.
- `ODDS_CREDIT_RESERVE` — default `50`; paid calls stop before crossing this floor.
- `ODDS_REQUEST_BUDGET` — default `8` HTTP odds requests per collection.
- `ODDS_MARKETS` — default `h2h,spreads,totals`.
- `ODDSRELAY_PRODUCTS` — default `standard`. Specialist products remain explicit opt-ins.
- `ODDSRELAY_TOKEN_BUDGET` — default `10000`. Unknown or over-budget quote estimates are not purchased.

Free discovery and quote calls happen before paid acquisition. Empty or unavailable markets can cost less than the conservative request-plan estimate; actual per-call credit headers are recorded.

The live zero-token quote check in run #83 (`36293353951`) returned these acquisition estimates: Standard 10,000; 2Up 300; Dutching 8,000; each-way 250; extra-place 100; BOG 100; Raw 6,271. The default remains Standard-only because it supplies the core cross-sport board. The specialist promotion and horse-racing products stay quote-visible but require an explicit usefulness decision before token expenditure.

## Schedule and health diagnostics

GitHub Actions prepares the **10:00** and **16:30 Europe/London** cycles through redundant non-round schedule opportunities: **09:42/09:57** and **16:12/16:27**. A repository-wide concurrency lock serializes them, and the locked freshness gate suppresses the later opportunity after the first one publishes a board. This provides scheduler redundancy without spending a second set of provider credits or tokens. Timezone-aware schedules preserve the local times across BST/GMT changes.

Because GitHub documents scheduled workflows as best-effort and may delay or drop them, `paper-lab/watchdog-trigger.json` is reserved for an independent event-driven recovery scheduler. A push carrying `[paper-lab-watchdog:morning]` or `[paper-lab-watchdog:evening]` enters the same concurrency lock and cycle freshness gate. If a scheduled run has already published, the watchdog push makes **no provider calls**; if the board is stale, it performs the one budgeted collection. Ordinary pushes remain inert.

The explicit morning cycle always ends at the following day's 10:00 London boundary, even though preparation starts just before 10:00. This avoids the previous three-minute-window edge case. Manual live runs still require the `collect_live_odds` workflow input. A maintainer can also perform a deliberate one-off end-to-end verification by including `[live-check]` in a commit message; ordinary pushes and pull requests run tests only.

`Paper Lab Health` runs at **10:08** and **16:38 Europe/London**, before the 10:15/16:45 analysis tasks. It reads only GitHub workflow metadata, the committed analysis board and the collection run's secret-free diagnostic artifact. It has no provider secrets and cannot contact OddsRelay or The Odds API. Its report distinguishes a missing scheduled event, test failure, provider/API failure, normalization failure, board/manifest failure, commit/publish failure and a fully successful fresh board. Every collection run uploads `paper-lab-diagnostic-<run-id>` even after a failed step.

Freshness now requires a cryptographically valid handoff pair: the board and the newest v0.5 manifest must share `observed_at_utc`, and the manifest's recorded board byte count and SHA-256 must match the committed file. Consumers can verify this without provider calls using `python paper-lab/pipeline_diagnostics.py validate-handoff --cycle morning --board paper-lab/data/v0.5/latest-analysis-board.json`.

The window starts at observation time and ends at the next 10:00 London boundary. The morning run covers the next 24 hours. The 16:30 refresh covers that evening and overnight through 10:00.

## Storage

Large per-cycle JSON is never added to ordinary Git history:

- raw and canonical payloads are compact gzip files under ignored `data/raw/`;
- both are uploaded as a 30-day workflow evidence artifact for debugging;
- canonical boards are also stored durably as monthly GitHub Release assets;
- `data/v0.5/latest-analysis-board.json` is a small rolling plain-text index that connected tools can read directly: every canonical event plus 160 sport-family-diversified complete market measurements;
- small committed manifests retain SHA-256 hashes, byte sizes, allowance usage and source/deduplication counts.

The existing 54 MB run #81 specimen remains in history because it is the first successful live-schema evidence. Future collections follow the external archive policy.

Run #85 (`36293695650`) proved the complete v0.5 path with real data. It used the bounded 10-credit/10,000-token cycle, produced 36 The Odds API rows plus 12,711 OddsRelay rows, reconciled 32 cross-provider duplicates into 12,715 canonical rows and uploaded both the short-retention evidence artifact and durable unified release asset. Its committed integrity record is `data/v0.5/manifest_2026-09-27T041401.958332_0000.json`.

Run #90 (`36342976765`) is the first recorded scheduled event. GitHub created it at 20:04 BST on 27 September, 3 hours 37 minutes after the intended 16:27 cycle. Once created, every job and publishing step succeeded. The run spent 10 The Odds API credits and acquired the quote-gated OddsRelay Standard product; its integrity record is `data/v0.5/manifest_2026-09-27T190423.279542_0000.json`. This incident is why schedule redundancy and the independent zero-cost health workflow exist.

The rolling analysis index is deliberately not the research conclusion or an automatic bet list. It gives the 10:15/16:45 analysis task connector-readable discovery coverage and bookmaker-dispersion measurements; full canonical evidence remains in release/artifact storage.

## Verification

Run the zero-credit suite with:

```bash
python -m unittest discover -s paper-lab -p "test_*.py"
```

Tests use mocks and compact fixtures only. They do not call either provider or consume credits/tokens.
