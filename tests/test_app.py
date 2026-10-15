from decimal import Decimal
from pathlib import Path

import pytest

from listaya.app import ConfigError, load_settings

D = Decimal


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    catalog = tmp_path / "catalogo.csv"
    catalog.write_text("sku,name,cost,price\n1,Martillo,10,20\n", encoding="utf-8")
    return {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "RADEON_API_KEY": "rc-test",
        "LISTAYA_CATALOG_PATH": str(catalog),
    }


class TestLoadSettings:
    def test_defaults(self, base_env):
        settings = load_settings(base_env)
        assert settings.telegram_token == "123:abc"
        assert settings.radeon_api_key == "rc-test"
        assert settings.allowed_chat_ids == frozenset()
        assert settings.db_path == Path("data/listaya.db")
        assert (settings.rule.target_margin, settings.rule.min_margin) == (D("0.30"), D("0.20"))
        assert settings.pricing.rounding_step == D("10")
        assert settings.pricing.prices_include_vat is False
        assert settings.pricing.currency == "ARS"
        assert settings.list_format.decimal_separator == ","
        assert (settings.text_model, settings.vision_model) == ("DeepSeek-V4-Flash", "DeepSeek-V4.1-Flash")

    def test_overrides(self, base_env):
        env = {
            **base_env,
            "TELEGRAM_ALLOWED_CHAT_IDS": " 42, 77 ,",
            "LISTAYA_TARGET_MARGIN": "0.35",
            "LISTAYA_MIN_MARGIN": "0.25",
            "LISTAYA_ROUNDING_STEP": "50",
            "LISTAYA_PRICES_INCLUDE_VAT": "true",
            "LISTAYA_TEXT_MODEL": "MiMo-V2.6-Flash",
        }
        settings = load_settings(env)
        assert settings.allowed_chat_ids == frozenset({"42", "77"})
        assert (settings.rule.target_margin, settings.rule.min_margin) == (D("0.35"), D("0.25"))
        assert settings.pricing.rounding_step == D("50")
        assert settings.pricing.prices_include_vat is True
        assert settings.text_model == "MiMo-V2.6-Flash"

    @pytest.mark.parametrize("token", ["", "replace-with-botfather-token"])
    def test_missing_or_placeholder_token_fails_clearly(self, base_env, token):
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            load_settings({**base_env, "TELEGRAM_BOT_TOKEN": token})

    def test_missing_api_key_fails_clearly(self, base_env):
        with pytest.raises(ConfigError, match="RADEON_API_KEY"):
            load_settings({**base_env, "RADEON_API_KEY": ""})

    def test_missing_catalog_points_to_the_sample(self, base_env, tmp_path):
        with pytest.raises(ConfigError, match="samples/ferreteria/catalogo.csv"):
            load_settings({**base_env, "LISTAYA_CATALOG_PATH": str(tmp_path / "nope.csv")})

    @pytest.mark.parametrize(
        "name, value",
        [
            ("LISTAYA_TARGET_MARGIN", "treinta"),
            ("LISTAYA_MIN_MARGIN", "0.5"),  # above the target margin
            ("LISTAYA_ROUNDING_STEP", "0"),
            ("LISTAYA_PRICES_INCLUDE_VAT", "quizas"),
            ("LISTAYA_DECIMAL_SEPARATOR", ";"),
        ],
    )
    def test_invalid_values_name_the_variable(self, base_env, name, value):
        with pytest.raises(ConfigError, match=name):
            load_settings({**base_env, name: value})
