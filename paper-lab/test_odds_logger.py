import csv
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import odds_logger


class OddsLoggerTests(unittest.TestCase):
    def test_default_market_config_is_h2h_only(self):
        self.assertEqual(odds_logger.default_market_keys(), ["h2h"])

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

        self.assertEqual([sport["key"] for sport in sports], ["soccer_epl", "basketball_nba", "americanfootball_nfl"])
        self.assertEqual(quota["requests_remaining"], "499")

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

            with patch.object(odds_logger, "DATA_DIR", data_dir):
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

            with patch.object(odds_logger, "DATA_DIR", data_dir):
                csv_path, _ = odds_logger.save_snapshot([row], {"schema_version": "0.2"})

            self.assertEqual(csv_path.name, "odds-2026-09-25.csv")
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["schema_version"], "0.2")


if __name__ == "__main__":
    unittest.main()
