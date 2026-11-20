import json

from src.data.polymarket_client import _outcome_token_ids


def test_up_down_tokens_map_from_labels_not_array_order():
    market = {
        "outcomes": json.dumps(["Down", "Up"]),
        "clobTokenIds": json.dumps(["down-token", "up-token"]),
    }
    assert _outcome_token_ids(market) == ("up-token", "down-token")


def test_yes_no_binary_market_maps_to_up_down_semantics():
    market = {
        "outcomes": ["No", "Yes"],
        "clobTokenIds": ["no-token", "yes-token"],
    }
    assert _outcome_token_ids(market) == ("yes-token", "no-token")


def test_unknown_or_ambiguous_outcomes_fail_closed():
    assert _outcome_token_ids({
        "outcomes": ["Higher", "Lower"],
        "clobTokenIds": ["a", "b"],
    }) == (None, None)
    assert _outcome_token_ids({
        "outcomes": ["Up", "Yes"],
        "clobTokenIds": ["a", "b"],
    }) == (None, None)


def test_misaligned_or_duplicate_token_ids_fail_closed():
    assert _outcome_token_ids({
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["only-one"],
    }) == (None, None)
    assert _outcome_token_ids({
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["same", "same"],
    }) == (None, None)
