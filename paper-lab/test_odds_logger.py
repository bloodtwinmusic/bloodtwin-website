import csv
import gzip
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import odds_logger


class OddsLoggerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "oddsrelay_standard_representative.json"
        cls.oddsrelay_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_provider_status_detects_both_keys_without_exposing_values(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": "secret-a", "ODDSRELAY_KEY": "secret-b"}, clear=False):
            self.assertEqual(odds_logger.provider_status(), {"the_odds_api": True, "oddsrelay": True})

    def test_missing_provider_secret_fails_without_revealing_values(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": "secret-a"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "oddsrelay"):
                odds_logger.require_provider_credentials()

    def test_readiness_output_never_prints_secret_values(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": "never-print-a", "ODDSRELAY_KEY": "never-print-b"}, clear=True), patch("builtins.print") as mocked_print:
            odds_logger.print_provider_readiness()
            rendered = " ".join(str(call) for call in mocked_print.call_args_list)
            self.assertIn("READY", rendered)
            self.assertNotIn("never-print-a", rendered)
            self.assertNotIn("never-print-b", rendered)

    def test_oddsrelay_discovery_requires_secret(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ODDSRELAY_KEY"):
                odds_logger.get_oddsrelay_sports()

    def test_oddsrelay_catalogue_uses_only_named_discovery_endpoints(self):
        calls = []
        def fake_get(endpoint, params=None):
            calls.append(endpoint)
            return [], {"tokens_cost": "0"}
        with patch.object(odds_logger, "oddsrelay_api_get", side_effect=fake_get):
            result = odds_logger.oddsrelay_discover_catalogue()
        self.assertEqual(set(result), {"sports", "bookmakers", "regions", "usage", "pricing"})
        self.assertEqual(calls, ["/v2/sports", "/v2/bookmakers", "/v2/regions", "/v2/usage", "/v2/pricing"])

    def test_oddsrelay_quote_forces_quote_mode(self):
        with patch.object(odds_logger, "oddsrelay_api_get", return_value=({"tokens": 123}, {"tokens_cost": "0"})) as mocked:
            odds_logger.oddsrelay_quote("standard", {"region": "uk"})
        mocked.assert_called_once_with("/v2/odds/standard", {"region": "uk", "quote": "true"})

    def test_oddsrelay_quote_rejects_unknown_product(self):
        with self.assertRaises(ValueError):
            odds_logger.oddsrelay_quote("mystery-board")

    def test_oddsrelay_window_uses_exact_paper_lab_boundaries(self):
        start = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
        params = odds_logger.oddsrelay_window_params(start, end)
        self.assertEqual(params["region"], "uk")
        self.assertEqual(params["commenceTimeFrom"], "2026-09-26T09:00:00+00:00".replace("+00:00", "Z"))
        self.assertEqual(params["commenceTimeTo"], "2026-09-27T09:00:00+00:00".replace("+00:00", "Z"))

    def test_oddsrelay_preflight_summary_never_contains_credentials(self):
        catalogue = {"sports": {"data": [{"key": "football"}], "usage": {"tokens_cost": "0"}}}
        quotes = {"standard": {"quote": {"tokens": 10000}, "usage": {"tokens_cost": "0"}}}
        summary = odds_logger.summarize_oddsrelay_preflight(catalogue, quotes)
        rendered = repr(summary)
        self.assertNotIn("ODDSRELAY_KEY", rendered)
        self.assertEqual(summary["quotes"]["standard"]["status"], "OK")
        self.assertEqual(summary["catalogue"]["sports"]["items"], 1)

    def test_oddsrelay_acquisition_plan_only_selects_successful_quotes(self):
        quotes = {
            "standard": {"quote": {"tokens": 10}, "usage": {"tokens_cost": "0"}},
            "raw": {"error": "RuntimeError", "detail": "bad request"},
        }
        self.assertEqual(odds_logger.oddsrelay_choose_products_from_quotes(quotes), ["standard"])

    def test_oddsrelay_quote_budget_selects_only_affordable_explicit_products(self):
        quotes = {
            "standard": {"quote": {"tokens": 10000}},
            "2up": {"quote": {"tokens": 25000}},
            "raw": {"quote": {"message": "cost unavailable"}},
        }
        selected, estimates = odds_logger.oddsrelay_select_products_from_quotes(
            quotes, ["standard", "2up", "raw"], token_budget=10000
        )
        self.assertEqual(selected, ["standard"])
        self.assertEqual(estimates, {"standard": 10000, "2up": 25000, "raw": None})

    def test_oddsrelay_executor_requires_explicit_allow_list(self):
        plan = {"products": ["standard", "raw"], "params": {"region": "uk"}}
        with patch.object(odds_logger, "oddsrelay_acquire_product") as mocked:
            result = odds_logger.oddsrelay_execute_plan(plan)
        self.assertEqual(result, {})
        mocked.assert_not_called()

    def test_oddsrelay_executor_cannot_acquire_unplanned_product(self):
        plan = {"products": ["standard"], "params": {"region": "uk"}}
        with patch.object(odds_logger, "oddsrelay_acquire_product", return_value=([], {})) as mocked:
            odds_logger.oddsrelay_execute_plan(plan, ["raw"])
        mocked.assert_not_called()

    def test_oddsrelay_snapshot_is_provider_aware_and_separate_schema(self):
        observed = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        snap = odds_logger.oddsrelay_snapshot_envelope(observed, observed, observed, {"standard": {"data": []}})
        self.assertEqual(snap["provider"], "oddsrelay")
        self.assertEqual(snap["schema_version"], "0.5")
        self.assertEqual(snap["version"], "0.5")

    def test_cross_provider_dedupe_keeps_newest_same_market(self):
        base = {"sport_key":"soccer","commence_time_utc":"2026-09-26T18:00:00Z","home":"A","away":"B","bookmaker_key":"book","market_key":"h2h","outcome_name":"A","point":None}
        old = dict(base, provider="the_odds_api", observed_at_utc="2026-09-26T10:00:00Z", price_decimal=2.0)
        new = dict(base, provider="oddsrelay", observed_at_utc="2026-09-26T10:01:00Z", price_decimal=2.1)
        result = odds_logger.dedupe_normalized_observations([old, new])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["price_decimal"], 2.1)

    def test_cross_provider_dedupe_preserves_distinct_lines(self):
        base = {"sport_key":"soccer","commence_time_utc":"2026-09-26T18:00:00Z","home":"A","away":"B","bookmaker_key":"book","market_key":"spreads","outcome_name":"A","observed_at_utc":"2026-09-26T10:00:00Z"}
        rows = [dict(base, point=-1.5), dict(base, point=-2.5)]
        self.assertEqual(len(odds_logger.dedupe_normalized_observations(rows)), 2)

    def test_oddsrelay_normalizer_handles_real_nested_standard_schema(self):
        rows = odds_logger.normalize_oddsrelay_payload(
            "standard", self.oddsrelay_fixture, "2026-09-26T17:51:09.140035Z"
        )
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["market_key"] for row in rows}, {"h2h", "totals"})
        self.assertEqual({row["price_side"] for row in rows}, {"back", "lay"})
        self.assertEqual({row["point"] for row in rows if row["market_key"] == "totals"}, {20.5})
        self.assertFalse(odds_logger.validate_canonical_rows(rows))

    def test_oddsrelay_normalizer_collapses_repeated_alias_lay_quote(self):
        rows = odds_logger.normalize_oddsrelay_payload(
            "standard", self.oddsrelay_fixture, "2026-09-26T17:51:09.140035Z"
        )
        home_lay = [
            row for row in rows
            if row["market_key"] == "h2h"
            and row["outcome_role"] == "home"
            and row["price_side"] == "lay"
        ]
        self.assertEqual(len(home_lay), 1)
        self.assertEqual(home_lay[0]["duplicate_count"], 2)

    def test_oddsrelay_normalizer_rejects_non_decimal_or_invalid_price(self):
        bad = json.loads(json.dumps(self.oddsrelay_fixture))
        bad["data"][0]["markets"][1]["outcomes"][0]["back"][0]["price"] = "EVS"
        rows = odds_logger.normalize_oddsrelay_payload("standard", bad, "2026-09-26T14:00:00Z")
        self.assertFalse(any(row["bookmaker_key"] == "fitzdares" and row["market_key"] == "h2h" for row in rows))

    def test_unified_snapshot_dedupes_cross_provider_overlap(self):
        observed = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        relay = odds_logger.normalize_oddsrelay_payload(
            "standard", self.oddsrelay_fixture, "2026-09-26T17:51:09.140035Z"
        )
        api_row = {
            "event_id": "toa-event-1",
            "observed_at_utc": "2026-09-26T17:51:09.140035Z",
            "sport_key": "tennis_wta_singapore_open",
            "sport_title": "WTA Singapore Open",
            "commence_time_utc": "2026-09-27T09:00:00Z",
            "home_team": "Leylah Fernandez",
            "away_team": "Talia Gibson",
            "bookmaker_key": "betfair_sb_uk",
            "bookmaker_title": "Betfair",
            "bookmaker_last_update": "2026-09-26T17:50:59Z",
            "market": "h2h",
            "outcome": "Leylah Fernandez",
            "point": None,
            "price_decimal": 1.52,
        }
        snap = odds_logger.unified_snapshot_envelope(
            observed, observed, observed, [api_row], relay, {"oddsrelay":"raw.json.gz"}
        )
        self.assertEqual(snap["source_counts"]["the_odds_api"], 1)
        self.assertEqual(snap["source_counts"]["oddsrelay"], 9)
        self.assertEqual(snap["source_counts"]["canonical"], 9)
        self.assertEqual(snap["source_counts"]["duplicates_collapsed"], 1)
        self.assertEqual(snap["source_counts"]["cross_provider_rows"], 1)
        overlap = [row for row in snap["observations"] if len(row["source_providers"]) == 2]
        self.assertEqual(overlap[0]["price_decimal"], 1.53)
        self.assertEqual(overlap[0]["home"], "Leylah Fernandez")
        self.assertEqual(snap["raw_source_refs"]["oddsrelay"], "raw.json.gz")

    def test_oddsrelay_default_acquisition_is_standard_only(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ODDSRELAY_PRODUCTS", None)
            self.assertEqual(odds_logger.default_oddsrelay_acquisition_products(), ["standard"])

    def test_oddsrelay_acquisition_products_reject_unknown(self):
        with patch.dict(os.environ, {"ODDSRELAY_PRODUCTS":"standard,mystery"}):
            with self.assertRaises(ValueError):
                odds_logger.default_oddsrelay_acquisition_products()

    def test_main_skips_oddsrelay_when_secret_absent(self):
        with patch.object(odds_logger, "oddsrelay_key_present", return_value=False), patch.object(odds_logger, "run_oddsrelay_collection") as relay:
            self.assertFalse(odds_logger.oddsrelay_key_present())
            relay.assert_not_called()

    def test_dual_provider_pipeline_can_be_exercised_without_network_or_spend(self):
        observed = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        plan = {"products":["standard"], "params":{"region":"uk"}}
        with (
            patch.object(odds_logger, "oddsrelay_build_acquisition_plan", return_value=plan),
            patch.object(odds_logger, "default_oddsrelay_acquisition_products", return_value=["standard"]),
            patch.object(odds_logger, "oddsrelay_execute_plan", return_value={"standard":{"data":[],"usage":{"tokens_cost":"10000"}}}),
            patch.object(odds_logger, "write_oddsrelay_snapshot", return_value=Path("raw.json")),
        ):
            rows, raw_path, returned_plan, acquisitions = odds_logger.run_oddsrelay_collection(observed, observed, observed)
        self.assertEqual(rows, [])
        self.assertEqual(raw_path, Path("raw.json"))
        self.assertEqual(returned_plan, plan)
        self.assertIn("standard", acquisitions)

    def test_default_market_config_includes_three_core_markets(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ODDS_MARKETS", None)
            self.assertEqual(odds_logger.default_market_keys(), ["h2h", "spreads", "totals"])

    def test_same_bookmaker_grouping_keeps_market_bounds(self):
        rows = [
            {
                "event_id": "evt-1",
                "bookmaker_key": "book-1",
                "market": "h2h",
                "outcome": "Home",
                "price_decimal": 2.0,
                "raw_implied_probability": 0.5,
            },
            {
                "event_id": "evt-1",
                "bookmaker_key": "book-1",
                "market": "h2h",
                "outcome": "Away",
                "price_decimal": 3.0,
                "raw_implied_probability": 1 / 3,
            },
            {
                "event_id": "evt-1",
                "bookmaker_key": "book-2",
                "market": "h2h",
                "outcome": "Home",
                "price_decimal": 1.9,
                "raw_implied_probability": 1 / 1.9,
            },
        ]

        grouped = odds_logger.compute_market_de_vigged_probabilities(rows)

        self.assertEqual(grouped[0]["market_de_vigged_probability"], 0.5 / (0.5 + (1 / 3)))
        self.assertEqual(grouped[1]["market_de_vigged_probability"], (1 / 3) / (0.5 + (1 / 3)))
        self.assertEqual(grouped[2]["market_de_vigged_probability"], 1)

    def test_matching_lines_are_grouped_for_spreads(self):
        rows = [
            {
                "event_id": "evt-2",
                "bookmaker_key": "book-1",
                "market": "spreads",
                "point": -3.5,
                "outcome": "Home",
                "price_decimal": 1.91,
                "raw_implied_probability": 1 / 1.91,
            },
            {
                "event_id": "evt-2",
                "bookmaker_key": "book-1",
                "market": "spreads",
                "point": 3.5,
                "outcome": "Away",
                "price_decimal": 1.98,
                "raw_implied_probability": 1 / 1.98,
            },
            {
                "event_id": "evt-2",
                "bookmaker_key": "book-1",
                "market": "spreads",
                "point": -4.5,
                "outcome": "Home",
                "price_decimal": 1.75,
                "raw_implied_probability": 1 / 1.75,
            },
        ]

        grouped = odds_logger.compute_market_de_vigged_probabilities(rows)

        self.assertEqual(
            grouped[0]["market_de_vigged_probability"],
            grouped[0]["raw_implied_probability"]
            / (grouped[0]["raw_implied_probability"] + grouped[1]["raw_implied_probability"]),
        )
        self.assertEqual(grouped[2]["market_de_vigged_probability"], 1.0)

    def test_sport_discovery_accepts_normal_active_payload(self):
        payload = [
            {"key": "soccer_epl", "group": "Soccer", "title": "English Premier League", "active": True},
            {"key": "basketball_nba", "group": "Basketball", "title": "NBA", "active": True},
            {"key": "americanfootball_nfl", "group": "American Football", "title": "NFL", "active": True},
            {"key": "boxing", "group": "Boxing", "title": "Boxing", "active": True},
            {"key": "tennis_wta", "group": "Tennis", "title": "WTA", "active": False},
        ]

        with patch.object(odds_logger, "api_get", return_value=(payload, {"requests_remaining": "499"})):
            sports, quota = odds_logger.get_active_sports()

        self.assertEqual([sport["key"] for sport in sports], ["boxing", "soccer_epl", "basketball_nba", "americanfootball_nfl"])
        self.assertEqual(quota["requests_remaining"], "499")

    def test_request_plan_excludes_outright_only_catalogue_entries(self):
        sports = [
            {"key": "golf_masters", "group": "Golf", "title": "Masters", "has_outrights": True},
            {"key": "soccer_epl", "group": "Soccer", "title": "EPL", "has_outrights": False},
        ]
        plan = odds_logger.plan_the_odds_api_requests(
            sports, credit_budget=1, max_requests=1, markets=["h2h"], cycle=0
        )
        self.assertEqual([item["sport"]["key"] for item in plan], ["soccer_epl"])

    def test_odds_api_http_error_retains_safe_quota_details(self):
        headers = {
            "x-requests-last": "0",
            "x-requests-used": "35",
            "x-requests-remaining": "465",
        }
        error = urllib.error.HTTPError(
            "https://redacted.invalid", 422, "Unprocessable Entity", headers, None
        )
        error.read = lambda: b'{"error_code":"INVALID_SPORT","message":"Sport is stale"}'
        with patch.dict(os.environ, {"ODDS_API_KEY": "never-expose"}), patch.object(
            urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaises(odds_logger.OddsAPIRequestError) as captured:
                odds_logger.api_get("/sports/stale/odds/")
        self.assertEqual(captured.exception.status, 422)
        self.assertEqual(captured.exception.quota["requests_remaining"], "465")
        self.assertNotIn("never-expose", str(captured.exception))

    def test_budget_selection_round_robins_across_sport_families(self):
        sports = [
            {"key": "soccer_a", "group": "Soccer", "title": "A Soccer"},
            {"key": "soccer_b", "group": "Soccer", "title": "B Soccer"},
            {"key": "soccer_c", "group": "Soccer", "title": "C Soccer"},
            {"key": "tennis_atp", "group": "Tennis", "title": "ATP"},
            {"key": "basketball_nba", "group": "Basketball", "title": "NBA"},
            {"key": "americanfootball_nfl", "group": "American Football", "title": "NFL"},
            {"key": "baseball_mlb", "group": "Baseball", "title": "MLB"},
            {"key": "icehockey_nhl", "group": "Ice Hockey", "title": "NHL"},
        ]

        selected = odds_logger.select_sports_for_budget(sports, 6, cycle=0)
        families = [odds_logger.sport_family(sport) for sport in selected]

        self.assertEqual(
            families,
            ["soccer", "tennis", "basketball", "american_football", "baseball", "ice_hockey"],
        )
        self.assertEqual(len(selected), 6)

    def test_budget_selection_cycles_after_family_coverage(self):
        sports = [
            {"key": "soccer_a", "group": "Soccer", "title": "A Soccer"},
            {"key": "soccer_b", "group": "Soccer", "title": "B Soccer"},
            {"key": "tennis_atp", "group": "Tennis", "title": "ATP"},
        ]
        selected = odds_logger.select_sports_for_budget(sports, 3, cycle=0)
        self.assertEqual([sport["key"] for sport in selected], ["soccer_a", "tennis_atp", "soccer_b"])

    def test_budget_selection_rotates_catalogue_between_cycles(self):
        sports = [
            {"key": "soccer_a", "group": "Soccer", "title": "A Soccer"},
            {"key": "soccer_b", "group": "Soccer", "title": "B Soccer"},
            {"key": "soccer_c", "group": "Soccer", "title": "C Soccer"},
        ]
        first = odds_logger.select_sports_for_budget(sports, 1, cycle=0)
        second = odds_logger.select_sports_for_budget(sports, 1, cycle=1)
        self.assertEqual(first[0]["key"], "soccer_a")
        self.assertEqual(second[0]["key"], "soccer_b")

    def test_request_plan_covers_three_markets_within_ten_credit_cap(self):
        sports = [
            {"key": f"soccer_{index}", "group": "Soccer", "title": f"Soccer {index}"}
            for index in range(12)
        ]
        plan = odds_logger.plan_the_odds_api_requests(
            sports, credit_budget=10, max_requests=10,
            markets=["h2h", "spreads", "totals"], cycle=0,
        )
        self.assertEqual(len(plan), 8)
        self.assertEqual(plan[0]["markets"], ["h2h", "spreads", "totals"])
        self.assertTrue(all(item["markets"] == ["h2h"] for item in plan[1:]))
        self.assertEqual(sum(item["estimated_credits"] for item in plan), 10)

    def test_raw_and_unified_snapshots_are_gzipped_outside_v05_manifest_dir(self):
        observed = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        snapshot = odds_logger.oddsrelay_snapshot_envelope(observed, observed, observed, {})
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir) / "raw"
            manifest_dir = Path(tmpdir) / "v0.5"
            raw_path = odds_logger.write_oddsrelay_snapshot(snapshot, raw_dir)
            self.assertEqual(raw_path.suffixes[-2:], [".json", ".gz"])
            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["provider"], "oddsrelay")
            manifest_path = odds_logger.write_collection_manifest(
                observed, [raw_path], {"test": True}, manifest_dir
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"][0]["filename"], raw_path.name)
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)

    def test_analysis_board_is_small_plain_text_with_broad_event_index(self):
        observed = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        relay = odds_logger.normalize_oddsrelay_payload(
            "standard", self.oddsrelay_fixture, "2026-09-26T17:51:09.140035Z"
        )
        unified = odds_logger.unified_snapshot_envelope(
            observed, observed, observed + timedelta(days=1), [], relay
        )
        board = odds_logger.build_analysis_board(unified)
        self.assertEqual(board["event_count"], 1)
        self.assertEqual(board["events"][0]["home"], "Fernandez, Leylah Annie")
        self.assertTrue(board["measured_markets"])
        self.assertNotIn("observations", board)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = odds_logger.write_analysis_board(unified, Path(tmpdir) / "latest-analysis-board.json")
            self.assertLess(path.stat().st_size, 10000)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], "0.5-analysis-1")

    def test_morning_window_ends_at_next_10am_london(self):
        now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)  # 10:00 BST
        start, end = odds_logger.default_collection_window(now)
        self.assertEqual(start, now)
        self.assertEqual(end, datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc))

    def test_preparation_cycle_just_before_ten_covers_the_following_day(self):
        now = datetime(2026, 9, 28, 8, 57, tzinfo=timezone.utc)  # 09:57 BST
        start, end = odds_logger.default_collection_window(now, cycle="morning")
        self.assertEqual(start, now)
        self.assertEqual(end, datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc))

    def test_unscheduled_pre_ten_window_keeps_legacy_next_boundary_semantics(self):
        now = datetime(2026, 9, 28, 8, 57, tzinfo=timezone.utc)  # 09:57 BST
        start, end = odds_logger.default_collection_window(now)
        self.assertEqual(start, now)
        self.assertEqual(end, datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))

    def test_evening_window_ends_at_next_10am_london(self):
        now = datetime(2026, 9, 25, 15, 30, tzinfo=timezone.utc)  # 16:30 BST
        start, end = odds_logger.default_collection_window(now)
        self.assertEqual(start, now)
        self.assertEqual(end, datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc))

    def test_window_handles_uk_dst_change(self):
        now = datetime(2026, 10, 24, 15, 30, tzinfo=timezone.utc)  # 16:30 BST
        start, end = odds_logger.default_collection_window(now)
        self.assertEqual(start, now)
        self.assertEqual(end, datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(hours=18, minutes=30))

    def test_explicit_window_filter_respects_boundaries(self):
        start = datetime(2026, 9, 25, 15, 30, tzinfo=timezone.utc)
        end = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
        events = [
            {"id": "before", "commence_time": "2026-09-25T15:29:59Z"},
            {"id": "start", "commence_time": "2026-09-25T15:30:00Z"},
            {"id": "inside", "commence_time": "2026-09-26T01:15:00Z"},
            {"id": "end", "commence_time": "2026-09-26T09:00:00Z"},
            {"id": "after", "commence_time": "2026-09-26T09:00:01Z"},
        ]
        filtered = odds_logger.filter_events_between(events, start, end)
        self.assertEqual([event["id"] for event in filtered], ["start", "inside", "end"])

    def test_collection_window_filters_events_not_sports(self):
        now = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        sport_payload = [{"key": "soccer_epl", "group": "Soccer", "title": "English Premier League", "active": True}]
        events = [
            {"id": "evt-1", "commence_time": "2026-09-25T11:00:00Z"},
            {"id": "evt-2", "commence_time": "2026-10-02T11:00:00Z"},
        ]

        filtered = odds_logger.filter_upcoming_events(events, now=now, start_hours=1, end_hours=168)
        self.assertEqual([event["id"] for event in filtered], ["evt-1"])
        self.assertEqual([sport["key"] for sport in sport_payload if sport["active"]], ["soccer_epl"])

    def test_save_snapshot_uses_versioned_csv_when_existing_header_is_incompatible(self):
        v1_header = [
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
        legacy_row = {
            "observed_at_utc": "2026-09-25T18:42:01.911829+00:00",
            "observed_at_london": "2026-09-25T19:42:01.911829+01:00",
            "event_id": "evt-legacy",
            "sport_key": "americanfootball_cfl",
            "sport_title": "CFL",
            "commence_time": "2026-09-26T00:00:00Z",
            "home_team": "Winnipeg Blue Bombers",
            "away_team": "Toronto Argonauts",
            "bookmaker_key": "leovegas",
            "bookmaker_title": "LeoVegas",
            "bookmaker_last_update": "2026-09-25T18:41:05Z",
            "market": "h2h",
            "outcome": "Toronto Argonauts",
            "price_decimal": 1.85,
            "point": "",
        }
        v2_row = {
            "observed_at_utc": "2026-09-25T20:20:00.134530+00:00",
            "observed_at_london": "2026-09-25T21:20:00.134530+01:00",
            "event_id": "evt-v2",
            "sport_key": "soccer_brazil_serie_b",
            "sport_title": "Brazil Série B",
            "commence_time_utc": "2026-09-29T22:30:00+00:00",
            "commence_time_london": "2026-09-29T23:30:00+01:00",
            "home_team": "Botafogo-SP",
            "away_team": "Ponte Preta",
            "bookmaker_key": "betway",
            "bookmaker_title": "Betway",
            "bookmaker_last_update": "2026-09-25T20:19:49Z",
            "market": "h2h",
            "outcome": "Botafogo-SP",
            "price_decimal": 1.22,
            "point": "",
            "raw_implied_probability": 0.819672131147541,
            "market_de_vigged_probability": 0.755810479775348,
            "schema_version": "0.2",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            legacy_path = data_dir / "odds-2026-09-25.csv"
            with legacy_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=v1_header)
                writer.writeheader()
                writer.writerow(legacy_row)

            with patch.object(odds_logger, "DATA_DIR", data_dir), patch.object(odds_logger, "datetime") as mock_datetime:
                mock_datetime.now.return_value = datetime(2026, 9, 25, 21, 20, tzinfo=timezone.utc).astimezone(odds_logger.LONDON)
                csv_path, metadata_path = odds_logger.save_snapshot([v2_row], {"schema_version": "0.2"})

            self.assertEqual(csv_path.name, "odds-2026-09-25-v0.2.csv")
            self.assertTrue(csv_path.exists())
            self.assertTrue(metadata_path.exists())
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            self.assertIn("schema_version", header)
            self.assertEqual(len(header), len(odds_logger.CSV_FIELDNAMES))
            with legacy_path.open("r", newline="", encoding="utf-8") as handle:
                legacy_rows = list(csv.reader(handle))
            self.assertEqual(len(legacy_rows), 2)

    def test_save_snapshot_appends_with_same_schema_to_default_csv(self):
        row = {
            "observed_at_utc": "2026-09-25T20:20:00.134530+00:00",
            "observed_at_london": "2026-09-25T21:20:00.134530+01:00",
            "event_id": "evt-v2-same",
            "sport_key": "soccer_brazil_serie_b",
            "sport_title": "Brazil Série B",
            "commence_time_utc": "2026-09-29T22:30:00+00:00",
            "commence_time_london": "2026-09-29T23:30:00+01:00",
            "home_team": "Botafogo-SP",
            "away_team": "Ponte Preta",
            "bookmaker_key": "betway",
            "bookmaker_title": "Betway",
            "bookmaker_last_update": "2026-09-25T20:19:49Z",
            "market": "h2h",
            "outcome": "Ponte Preta",
            "price_decimal": 11.0,
            "point": "",
            "raw_implied_probability": 0.09090909090909091,
            "market_de_vigged_probability": 0.0838262532114477,
            "schema_version": "0.2",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            target_path = data_dir / "odds-2026-09-25.csv"
            with target_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=odds_logger.CSV_FIELDNAMES)
                writer.writeheader()
                writer.writerow(row)

            with patch.object(odds_logger, "DATA_DIR", data_dir), patch.object(odds_logger, "datetime") as mock_datetime:
                mock_datetime.now.return_value = datetime(2026, 9, 25, 21, 20, tzinfo=timezone.utc).astimezone(odds_logger.LONDON)
                csv_path, _ = odds_logger.save_snapshot([row], {"schema_version": "0.2"})

            self.assertEqual(csv_path.name, "odds-2026-09-25.csv")
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["schema_version"], "0.2")


if __name__ == "__main__":
    unittest.main()
