import pytest

from config import parse_amount


@pytest.mark.parametrize("text,expected", [
    ("50000", 50000), ("50,000", 50000), ("50k", 50000), ("50K", 50000),
    ("1", 1), ("1.5k", 1500), (" 2,500 ", 2500),
])
def test_valid(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["0", "-1", "50001", "51k", "abc", "", None, "5,00", "1.5", "0.0001k"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_amount(text)
