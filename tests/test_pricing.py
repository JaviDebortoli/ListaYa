from decimal import Decimal

import pytest

from listaya.pricing import (
    MarginRule,
    PricingConfig,
    UnknownVatError,
    convert_vat,
    margin,
    quote,
    resolve_rule,
    round_price,
)

D = Decimal
RULE = MarginRule(target_margin=D("0.30"), min_margin=D("0.20"))
CONFIG = PricingConfig()


def q4(value: Decimal) -> Decimal:
    return value.quantize(D("0.0001"))


class TestMarginRule:
    def test_rejects_target_margin_of_one_or_more(self):
        with pytest.raises(ValueError):
            MarginRule(target_margin=D("1"), min_margin=D("0.2"))

    def test_rejects_negative_margins(self):
        with pytest.raises(ValueError):
            MarginRule(target_margin=D("0.3"), min_margin=D("-0.1"))

    def test_rejects_min_margin_above_target(self):
        with pytest.raises(ValueError):
            MarginRule(target_margin=D("0.2"), min_margin=D("0.3"))


class TestResolveRule:
    def test_product_rule_wins_over_category_and_default(self):
        product_rule = MarginRule(D("0.40"), D("0.25"))
        category_rule = MarginRule(D("0.35"), D("0.20"))
        resolved = resolve_rule(
            product_id="p1",
            category="tornillos",
            product_rules={"p1": product_rule},
            category_rules={"tornillos": category_rule},
            default=RULE,
        )
        assert resolved is product_rule

    def test_category_rule_wins_over_default(self):
        category_rule = MarginRule(D("0.35"), D("0.20"))
        resolved = resolve_rule(
            product_id="p1",
            category="tornillos",
            product_rules={},
            category_rules={"tornillos": category_rule},
            default=RULE,
        )
        assert resolved is category_rule

    def test_falls_back_to_default(self):
        resolved = resolve_rule(
            product_id="p1",
            category=None,
            product_rules={},
            category_rules={},
            default=RULE,
        )
        assert resolved is RULE


class TestMarginAndRounding:
    def test_margin_is_computed_over_selling_price(self):
        assert margin(price=D("100"), cost=D("70")) == D("0.30")

    def test_rounds_to_nearest_multiple_of_step(self):
        assert round_price(D("1414"), step=D("10")) == D("1410")
        assert round_price(D("1415"), step=D("10")) == D("1420")

    def test_rounding_step_is_configurable(self):
        assert round_price(D("1449"), step=D("100")) == D("1400")

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            round_price(D("100"), step=D("0"))


class TestConvertVat:
    def test_same_convention_keeps_amount(self):
        assert convert_vat(D("100"), includes_vat=False, target_includes_vat=False, rate=D("0.21")) == D("100")

    def test_removes_vat(self):
        assert convert_vat(D("121"), includes_vat=True, target_includes_vat=False, rate=D("0.21")) == D("100")

    def test_adds_vat(self):
        assert convert_vat(D("100"), includes_vat=False, target_includes_vat=True, rate=D("0.21")) == D("121")

    def test_unknown_vat_is_never_assumed(self):
        with pytest.raises(UnknownVatError):
            convert_vat(D("100"), includes_vat=None, target_includes_vat=False, rate=D("0.21"))


class TestQuote:
    def test_suggested_price_uses_target_margin_and_rounding(self):
        # 1000 / (1 - 0.30) = 1428.57 -> nearest multiple of 10 = 1430
        result = quote(new_cost=D("1000"), current_price=D("1200"), current_cost=D("850"),
                       currency="ARS", rule=RULE, config=CONFIG)
        assert result.priced
        assert result.suggested_price == D("1430")
        assert q4(result.variation) == D("0.1917")  # (1430 - 1200) / 1200
        assert q4(result.cost_change) == D("0.1765")  # (1000 - 850) / 850
        assert result.flags == frozenset()

    def test_alert_when_current_price_falls_below_min_margin_with_new_cost(self):
        # margin of current price with new cost: (1200 - 1000) / 1200 = 0.1667 < 0.20
        result = quote(new_cost=D("1000"), current_price=D("1200"), current_cost=D("850"),
                       currency="ARS", rule=RULE, config=CONFIG)
        assert q4(result.current_margin) == D("0.1667")
        assert result.below_min_margin is True

    def test_no_alert_when_current_price_keeps_min_margin(self):
        result = quote(new_cost=D("900"), current_price=D("1200"), current_cost=D("850"),
                       currency="ARS", rule=RULE, config=CONFIG)
        assert result.below_min_margin is False

    def test_margin_exactly_at_minimum_is_not_an_alert(self):
        result = quote(new_cost=D("800"), current_price=D("1000"), current_cost=D("800"),
                       currency="ARS", rule=RULE, config=CONFIG)
        assert result.current_margin == D("0.20")
        assert result.below_min_margin is False

    def test_rounding_never_drops_suggested_price_below_min_margin(self):
        # 6 / 0.7 = 8.57 -> nearest 10 would be 10 (ok); 3 / 0.7 = 4.29 -> nearest 10 is 0, so round up to 10
        rule = MarginRule(D("0.30"), D("0.20"))
        result = quote(new_cost=D("3"), current_price=None, current_cost=None,
                       currency="ARS", rule=rule, config=CONFIG)
        assert result.suggested_price == D("10")

    def test_usd_cost_is_reported_not_priced(self):
        result = quote(new_cost=D("10"), current_price=D("1200"), current_cost=D("850"),
                       currency="USD", rule=RULE, config=CONFIG)
        assert not result.priced
        assert result.suggested_price is None
        assert result.below_min_margin is None
        assert "foreign_currency" in result.flags

    def test_unknown_currency_is_reported_not_priced(self):
        result = quote(new_cost=D("10"), current_price=D("1200"), current_cost=D("850"),
                       currency=None, rule=RULE, config=CONFIG)
        assert not result.priced
        assert "unknown_currency" in result.flags

    def test_product_without_previous_cost_is_flagged(self):
        result = quote(new_cost=D("1000"), current_price=D("1500"), current_cost=None,
                       currency="ARS", rule=RULE, config=CONFIG)
        assert result.priced
        assert result.cost_change is None
        assert "no_previous_cost" in result.flags

    def test_product_without_current_price_has_no_variation_or_alert(self):
        result = quote(new_cost=D("1000"), current_price=None, current_cost=D("900"),
                       currency="ARS", rule=RULE, config=CONFIG)
        assert result.suggested_price == D("1430")
        assert result.variation is None
        assert result.below_min_margin is None
        assert "no_current_price" in result.flags

    def test_rejects_non_positive_cost(self):
        with pytest.raises(ValueError):
            quote(new_cost=D("0"), current_price=D("100"), current_cost=None,
                  currency="ARS", rule=RULE, config=CONFIG)
