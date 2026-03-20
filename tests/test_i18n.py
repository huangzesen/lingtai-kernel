"""Tests for stoai_kernel.i18n."""
from stoai_kernel.i18n import t


class TestT:

    def test_simple_key(self):
        assert t("en", "soul.time_lapse", seconds=120) == "120 seconds passed, cherish your time, take initiative."

    def test_chinese_key(self):
        assert t("zh", "soul.time_lapse", seconds=120) == "已过去120秒,珍惜时光，发挥你的主观能动性"

    def test_template_substitution(self):
        result = t("en", "system.current_time", time="2026-03-19T00:00:00Z")
        assert "[Current time: 2026-03-19T00:00:00Z]" in result

    def test_chinese_template(self):
        result = t("zh", "system.current_time", time="2026-03-19T00:00:00Z")
        assert "2026-03-19T00:00:00Z" in result

    def test_unknown_lang_falls_back_to_en(self):
        assert t("xx", "soul.time_lapse", seconds=60) == "60 seconds passed, cherish your time, take initiative."

    def test_unknown_key_returns_key(self):
        result = t("en", "nonexistent.key")
        assert result == "nonexistent.key"
