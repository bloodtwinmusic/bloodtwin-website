import sys
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


if __name__ == "__main__":
    unittest.main()
