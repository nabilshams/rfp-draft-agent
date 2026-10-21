"""Regex-based redaction engine driven by a YAML policy file.

Used during:
* Knowledge-base ingestion (sanitize source docs before indexing)
* Pre-generation pass (scrub user input / retrieved snippets)
* Post-generation scan (block responses containing sensitive content)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "redaction_policy.yml"
_CUSTOMER_RULE_NAME = "customer_name"
_MIN_CUSTOMER_NAME_LENGTH = 2  # a one-letter "name" would redact half of every document


@dataclass
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str
    enabled: bool = True


@dataclass
class RedactionResult:
    """Outcome of running text through the redaction engine."""

    original_hash: str
    sanitized_text: str
    redactions: list[dict] = field(default_factory=list)  # [{rule, span_start, span_end, matched}]
    redaction_count: int = 0


class RedactionEngine:
    """Configurable, auditable redaction engine."""

    def __init__(
        self,
        policy_path: str | Path | None = None,
        customer_allowlist: list[str] | None = None,
        customer_names: list[str] | None = None,
    ) -> None:
        policy_path = Path(policy_path) if policy_path else _DEFAULT_POLICY_PATH
        self.rules: list[RedactionRule] = []
        self.customer_allowlist: set[str] = set(customer_allowlist or [])
        self.customer_names: list[str] = []
        self._load_policy(policy_path)
        self.customer_names.extend(customer_names or [])
        self._rebuild_customer_rule()

    # ── public API ────────────────────────────────────────

    def redact(self, text: str) -> RedactionResult:
        """Apply all enabled rules to *text* and return a :class:`RedactionResult`."""
        original_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        redactions: list[dict] = []
        sanitized = text

        for rule in self.rules:
            if not rule.enabled:
                continue
            for m in rule.pattern.finditer(sanitized):
                redactions.append(
                    {
                        "rule": rule.name,
                        "span_start": m.start(),
                        "span_end": m.end(),
                        "matched": m.group()[:6] + "…",  # truncated for safety
                    }
                )
            sanitized = rule.pattern.sub(rule.replacement, sanitized)

        result = RedactionResult(
            original_hash=original_hash,
            sanitized_text=sanitized,
            redactions=redactions,
            redaction_count=len(redactions),
        )

        if redactions:
            logger.info("Redacted %d patterns (hash=%s)", len(redactions), original_hash)

        return result

    def scan_output(self, text: str) -> list[dict]:
        """Scan generated text for any remaining sensitive patterns.

        Returns a list of violations – if non-empty the caller should block the
        output and flag for human review.
        """
        violations: list[dict] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            for m in rule.pattern.finditer(text):
                violations.append({"rule": rule.name, "matched_preview": m.group()[:8] + "…"})
        return violations

    def is_customer_name_allowed(self, name: str) -> bool:
        return name.lower() in {n.lower() for n in self.customer_allowlist}

    # ── internals ─────────────────────────────────────────

    def _load_policy(self, path: Path) -> None:
        if not path.exists():
            logger.warning("Redaction policy not found at %s – no rules loaded", path)
            return

        with open(path, encoding="utf-8") as f:
            policy = yaml.safe_load(f)

        for entry in policy.get("rules", []):
            try:
                flags = re.IGNORECASE if entry.get("ignore_case", True) else 0
                compiled = re.compile(entry["pattern"], flags)
                self.rules.append(
                    RedactionRule(
                        name=entry["name"],
                        pattern=compiled,
                        replacement=entry.get("replacement", "[REDACTED]"),
                        enabled=entry.get("enabled", True),
                    )
                )
            except re.error as exc:
                logger.error("Invalid regex in rule '%s': %s", entry["name"], exc)

        allowlist = policy.get("customer_allowlist", [])
        if allowlist:
            self.customer_allowlist.update(allowlist)
        self.customer_names.extend(policy.get("customer_names") or [])

        logger.info("Loaded %d redaction rules from %s", len(self.rules), path)

    def _rebuild_customer_rule(self) -> None:
        """(Re)build the customer-name rule from the protected names minus the allow-list.

        Names are matched case-insensitively as whole words. Names are never logged, only their count.
        """
        self.rules = [r for r in self.rules if r.name != _CUSTOMER_RULE_NAME]
        allowed = {n.lower() for n in self.customer_allowlist}
        names = {n.strip() for n in self.customer_names if n and n.strip()}
        too_short = {n for n in names if len(n) < _MIN_CUSTOMER_NAME_LENGTH}
        if too_short:
            logger.warning("Ignoring %d customer name(s) shorter than %d characters", len(too_short), _MIN_CUSTOMER_NAME_LENGTH)
        active = sorted((n for n in names - too_short if n.lower() not in allowed), key=len, reverse=True)
        if not active:
            return
        alternatives = "|".join(re.escape(n) for n in active)
        self.rules.append(
            RedactionRule(
                name=_CUSTOMER_RULE_NAME,
                pattern=re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE),
                replacement="[REDACTED-CUSTOMER]",
            )
        )
        logger.info("Protecting %d customer name(s)", len(active))


# Module-level singleton (lazy)
_engine: RedactionEngine | None = None


def get_redaction_engine() -> RedactionEngine:
    global _engine
    if _engine is None:
        # Customer names come from the environment (not the policy file) so they never have to be committed.
        names = [n.strip() for n in get_settings().redaction_customer_names.split(";") if n.strip()]
        _engine = RedactionEngine(customer_names=names)
    return _engine
