import pytest

from listaya.extraction.rows import ListFormat
from listaya.extraction_check import build_parser, list_format_from_args


def test_defaults_keep_the_argentine_format():
    args = build_parser().parse_args(["lista.pdf"])
    assert list_format_from_args(args) == ListFormat(decimal_separator=",", local_currency="ARS")


def test_currency_and_decimal_separator_options_build_the_list_format():
    args = build_parser().parse_args(["catalog.csv", "--currency", "USD", "--decimal-separator", "."])
    assert list_format_from_args(args) == ListFormat(decimal_separator=".", local_currency="USD")


@pytest.mark.parametrize("option", [["--currency", "EUR"], ["--decimal-separator", ";"]])
def test_invalid_options_are_rejected(option):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["lista.pdf", *option])
