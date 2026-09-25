import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import odds_logger


def test_default_market_config_is_h2h_only():
    assert odds_logger.default_market_keys() == ["h2h"]


def test_same_bookmaker_grouping_keeps_market_bounds():
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

    assert grouped[0]["market_de_vigged_probability"] == 0.5 / (0.5 + (1 / 3))
    assert grouped[1]["market_de_vigged_probability"] == (1 / 3) / (0.5 + (1 / 3))
    assert grouped[2]["market_de_vigged_probability"] == 1


def test_matching_lines_are_grouped_for_spreads():
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

    assert grouped[0]["market_de_vigged_probability"] == grouped[0]["raw_implied_probability"] / (
        grouped[0]["raw_implied_probability"] + grouped[1]["raw_implied_probability"]
    )
    assert grouped[2]["market_de_vigged_probability"] == 1.0
