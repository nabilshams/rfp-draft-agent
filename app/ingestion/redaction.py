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

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "redaction_policy.yml"


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

    def __init__(self, policy_path: str | Path | None = None, customer_allowlist: list[str] | None = None) -> None:
        policy_path = Path(policy_path) if policy_path else _DEFAULT_POLICY_PATH
        self.rules: list[RedactionRule] = []
        self.customer_allowlist: set[str] = set(customer_allowlist or [])
        self._load_policy(policy_path)

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
                compiled = re.compile(entry["pattern"], re.IGNORECASE)
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

        logger.info("Loaded %d redaction rules from %s", len(self.rules), path)


# Module-level singleton (lazy)
_engine: RedactionEngine | None = None


def get_redaction_engine() -> RedactionEngine:
    global _engine
    if _engine is None:
        _engine = RedactionEngine()
    return _engine
