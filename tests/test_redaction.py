"""Tests for the redaction engine."""

import pytest

from app.ingestion.redaction import RedactionEngine


@pytest.fixture
def engine() -> RedactionEngine:
    """Provide a default redaction engine loaded from the project policy file."""
    return RedactionEngine()


# ── Pricing patterns ─────────────────────────────────────


class TestPricingRedaction:
    def test_usd_price_is_redacted(self, engine: RedactionEngine):
        text = "The total cost is $1,250.00 per year."
        result = engine.redact(text)
        assert "$1,250.00" not in result.sanitized_text
        assert "[REDACTED-PRICE]" in result.sanitized_text
        assert result.redaction_count >= 1

    def test_eur_price_is_redacted(self, engine: RedactionEngine):
        text = "Our offer is €500 for the initial phase."
        result = engine.redact(text)
        assert "€500" not in result.sanitized_text
        assert "[REDACTED-PRICE]" in result.sanitized_text

    def test_gbp_price_is_redacted(self, engine: RedactionEngine):
        text = "Budget allocation: £12,000.50."
        result = engine.redact(text)
        assert "£12,000.50" not in result.sanitized_text

    def test_currency_code_is_redacted(self, engine: RedactionEngine):
        text = "The price is 3,400 USD for the engagement."
        result = engine.redact(text)
        assert "3,400 USD" not in result.sanitized_text

    def test_no_false_positive_on_normal_numbers(self, engine: RedactionEngine):
        text = "Section 3.2 describes 15 requirements."
        result = engine.redact(text)
        assert result.redaction_count == 0


# ── PII patterns ─────────────────────────────────────────


class TestPIIRedaction:
    def test_email_is_redacted(self, engine: RedactionEngine):
        text = "Contact john.doe@example.com for details."
        result = engine.redact(text)
        assert "john.doe@example.com" not in result.sanitized_text
        assert "[REDACTED-EMAIL]" in result.sanitized_text

    def test_us_phone_is_redacted(self, engine: RedactionEngine):
        text = "Call us at (555) 123-4567 for support."
        result = engine.redact(text)
        assert "(555) 123-4567" not in result.sanitized_text
        assert "[REDACTED-PHONE]" in result.sanitized_text

    def test_ssn_is_redacted(self, engine: RedactionEngine):
        text = "SSN: 123-45-6789 on file."
        result = engine.redact(text)
        assert "123-45-6789" not in result.sanitized_text
        assert "[REDACTED-SSN]" in result.sanitized_text

    def test_street_address_is_redacted(self, engine: RedactionEngine):
        text = "Our office is at 123 Main Street in Springfield."
        result = engine.redact(text)
        assert "123 Main Street" not in result.sanitized_text

    def test_intl_phone_is_redacted(self, engine: RedactionEngine):
        text = "Reach us at +44-20-7946-0958."
        result = engine.redact(text)
        assert "+44-20-7946-0958" not in result.sanitized_text


# ── Banking info ─────────────────────────────────────────


class TestBankingRedaction:
    def test_credit_card_is_redacted(self, engine: RedactionEngine):
        text = "Card: 4111-1111-1111-1111 for billing."
        result = engine.redact(text)
        assert "4111-1111-1111-1111" not in result.sanitized_text
        assert "[REDACTED-CC]" in result.sanitized_text

    def test_iban_is_redacted(self, engine: RedactionEngine):
        text = "Wire to IBAN: GB29 NWBK 6016 1331 9268 19."
        result = engine.redact(text)
        assert "GB29" not in result.sanitized_text


# ── Output scanning ──────────────────────────────────────


class TestOutputScanning:
    def test_scan_detects_pricing(self, engine: RedactionEngine):
        text = "We propose a fee of $50,000 for this project."
        violations = engine.scan_output(text)
        assert len(violations) > 0
        assert any(v["rule"] == "currency_usd" for v in violations)

    def test_scan_clean_text_no_violations(self, engine: RedactionEngine):
        text = "Our solution meets all compliance requirements."
        violations = engine.scan_output(text)
        assert len(violations) == 0


# ── Hash tracking ────────────────────────────────────────


class TestRedactionMetadata:
    def test_original_hash_is_present(self, engine: RedactionEngine):
        result = engine.redact("Test text with $100 price.")
        assert result.original_hash
        assert len(result.original_hash) == 16

    def test_redaction_list_has_entries(self, engine: RedactionEngine):
        result = engine.redact("Email: a@b.com, price $99.")
        assert len(result.redactions) >= 2
        rule_names = {r["rule"] for r in result.redactions}
        assert "email" in rule_names
        assert "currency_usd" in rule_names
