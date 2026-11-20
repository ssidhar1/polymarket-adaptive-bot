from src.data.resolution import _extract_from_market, _parse_labeled_outcome_prices


def test_outcome_prices_are_mapped_by_labels_not_array_order():
    up, down = _parse_labeled_outcome_prices(
        '["Down", "Up"]',
        '["1", "0"]',
    )
    assert up == 0.0
    assert down == 1.0


def test_unknown_or_ambiguous_resolution_labels_fail_closed():
    assert _parse_labeled_outcome_prices(
        ["Higher", "Lower"], ["1", "0"]
    ) == (None, None)
    assert _parse_labeled_outcome_prices(
        ["Up", "Yes"], ["1", "0"]
    ) == (None, None)


def test_resolver_uses_labeled_token_winners_when_prices_missing():
    closed, up, down = _extract_from_market({
        "closed": True,
        "tokens": [
            {"outcome": "Down", "winner": True},
            {"outcome": "Up", "winner": False},
        ],
    })
    assert closed is True
    assert up == 0.0
    assert down == 1.0


def test_resolver_does_not_guess_from_token_array_position():
    closed, up, down = _extract_from_market({
        "closed": True,
        "tokens": [
            {"winner": True, "price": 1.0},
            {"winner": False, "price": 0.0},
        ],
    })
    assert closed is False
    assert up is None and down is None
