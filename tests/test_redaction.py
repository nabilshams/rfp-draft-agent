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


# ── Tuned rules: realistic RFP text ──────────────────────


class TestNoFalsePositives:
    """Ordinary RFP content that earlier versions of the rules redacted."""

    @pytest.mark.parametrize(
        "text",
        [
            "The platform shall support 99.9% availability per Section 3.2.1.",
            "Provide ISO 27001 and SOC 2 Type II certifications.",
            "Throughput of 1234567890 requests per day is required.",
            "Our database holds 123456789 records across 4 regions.",
            "Dr. Patel supervises 50 beds supervised by Dr. Jones.",
            "Allow 24 hours of support from the Service Desk Dr. in Sydney.",
            "Version 10.0.19045.1234 and build 2024-03-15 are supported.",
            "Azure region eastus2 with SKU Standard_D4s_v3 is required.",
            "We offer 5 years of experience.",
            "Requirement F12: support 10000 concurrent users.",
        ],
    )
    def test_ordinary_text_is_left_alone(self, engine: RedactionEngine, text):
        result = engine.redact(text)
        assert result.sanitized_text == text, result.redactions


class TestNoFalseNegatives:
    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("Call +1 800 555 0199 for help.", "555 0199"),
            ("Call (555) 123-4567 for help.", "123-4567"),
            ("Call 555-123-4567 for help.", "555-123-4567"),
            ("Call 555.123.4567 for help.", "555.123.4567"),
            ("Phone: 5551234567", "5551234567"),
            ("Fax 555 123 4567", "555 123 4567"),
            ("Reach us at +44-20-7946-0958.", "7946"),
            ("The ABA routing number 021000021 is for the bank.", "021000021"),
            ("Routing: 021000021", "021000021"),
            ("Our office is at 10 Downing Street.", "Downing"),
            ("Visit 221 Baker St for details.", "Baker"),
            ("Passport AB1234567 was provided.", "AB1234567"),
            ("Pay to GB29 NWBK 6016 1331 9268 19.", "NWBK"),
            ("Card 4111 1111 1111 1111 on file.", "4111 1111"),
        ],
    )
    def test_sensitive_values_are_removed(self, engine: RedactionEngine, text, secret):
        assert secret not in engine.redact(text).sanitized_text

    def test_phone_numbers_are_replaced_whole(self, engine: RedactionEngine):
        result = engine.redact("Call +1 800 555 0199 or (555) 123-4567.")
        assert result.sanitized_text == "Call [REDACTED-PHONE] or [REDACTED-PHONE]."

    def test_a_sentence_ending_period_after_an_address_is_kept(self, engine: RedactionEngine):
        assert engine.redact("Based at 10 Downing Street.").sanitized_text == "Based at [REDACTED-ADDRESS]."


class TestCaseSensitiveRules:
    def test_rules_can_opt_out_of_case_insensitivity(self, tmp_path):
        policy = tmp_path / "policy.yml"
        policy.write_text(
            "rules:\n"
            "  - name: code\n    pattern: 'ABC\\d+'\n    replacement: '[X]'\n    ignore_case: false\n"
            "  - name: any_case\n    pattern: 'xyz\\d+'\n    replacement: '[Y]'\n",
            encoding="utf-8",
        )
        engine = RedactionEngine(policy_path=policy)
        assert engine.redact("ABC12 abc12 XYZ9 xyz9").sanitized_text == "[X] abc12 [Y] [Y]"


# ── Customer names ───────────────────────────────────────


class TestCustomerNames:
    def engine(self, *names, allowlist=()):
        return RedactionEngine(customer_names=list(names), customer_allowlist=list(allowlist))

    def test_listed_customers_are_redacted_case_insensitively(self):
        result = self.engine("Contoso").redact("Contoso asked; CONTOSO and contoso agreed.")
        assert result.sanitized_text == "[REDACTED-CUSTOMER] asked; [REDACTED-CUSTOMER] and [REDACTED-CUSTOMER] agreed."
        assert {r["rule"] for r in result.redactions} == {"customer_name"}

    def test_only_whole_words_match(self):
        text = "Contosoville and Fabrikamish are unrelated; the Contoso's bid is not."
        assert self.engine("Contoso", "Fabrikam").redact(text).sanitized_text == (
            "Contosoville and Fabrikamish are unrelated; the [REDACTED-CUSTOMER]'s bid is not."
        )

    def test_names_with_punctuation_are_matched_literally(self):
        engine = self.engine("AT&T", "Smith (UK) Ltd", "C++ Corp")
        text = "AT&T, Smith (UK) Ltd. and C++ Corp signed."
        assert engine.redact(text).sanitized_text == "[REDACTED-CUSTOMER], [REDACTED-CUSTOMER]. and [REDACTED-CUSTOMER] signed."

    def test_longer_names_win_over_their_prefixes(self):
        result = self.engine("Contoso", "Contoso Pharmaceuticals").redact("Contoso Pharmaceuticals and Contoso.")
        assert result.sanitized_text == "[REDACTED-CUSTOMER] and [REDACTED-CUSTOMER]."

    def test_the_allowlist_exempts_the_customer_being_addressed(self):
        engine = self.engine("Contoso", "Fabrikam", allowlist=["contoso"])
        assert engine.redact("Contoso and Fabrikam").sanitized_text == "Contoso and [REDACTED-CUSTOMER]"

    def test_output_scanning_flags_customer_names(self):
        violations = self.engine("Contoso").scan_output("As we did for Contoso last year")
        assert [v["rule"] for v in violations] == ["customer_name"]

    def test_no_names_means_no_rule(self):
        engine = self.engine()
        assert engine.redact("Contoso").sanitized_text == "Contoso"
        assert "customer_name" not in {r.name for r in engine.rules}

    @pytest.mark.parametrize("bad", ["", " ", "A"])
    def test_blank_or_one_letter_names_are_ignored(self, bad):
        assert self.engine(bad).redact("A B C").sanitized_text == "A B C"

    def test_names_from_the_policy_file_are_supported_for_private_forks(self, tmp_path):
        policy = tmp_path / "policy.yml"
        policy.write_text("rules: []\ncustomer_names: ['Contoso']\ncustomer_allowlist: []\n", encoding="utf-8")
        assert RedactionEngine(policy_path=policy).redact("Contoso").sanitized_text == "[REDACTED-CUSTOMER]"

    def test_the_shipped_policy_names_no_customers(self):
        assert RedactionEngine().customer_names == []  # the repository is public

    def test_names_are_never_logged(self, caplog):
        with caplog.at_level("DEBUG", logger="app.ingestion.redaction"):
            self.engine("Contoso").redact("Contoso signed")
        assert "Contoso" not in caplog.text
        assert "Protecting 1 customer name(s)" in caplog.text


class TestEngineFromSettings:
    def test_names_come_from_the_environment_setting(self, monkeypatch):
        from types import SimpleNamespace

        from app.ingestion import redaction

        monkeypatch.setattr(redaction, "_engine", None)
        monkeypatch.setattr(redaction, "get_settings", lambda: SimpleNamespace(redaction_customer_names="Contoso; Fabrikam Ltd ;;"))

        engine = redaction.get_redaction_engine()

        assert engine.redact("Contoso and Fabrikam Ltd").sanitized_text == "[REDACTED-CUSTOMER] and [REDACTED-CUSTOMER]"
        assert redaction.get_redaction_engine() is engine  # cached

    def test_the_default_is_no_customers(self, monkeypatch):
        from app.ingestion import redaction

        monkeypatch.setattr(redaction, "_engine", None)
        assert redaction.get_redaction_engine().customer_names == []
