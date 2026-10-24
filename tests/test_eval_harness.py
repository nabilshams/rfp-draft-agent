"""The evaluation harness: scoring, the golden set, replay, the regression gate and the degraded-prompt proof."""

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

from app.agents import states
from app.evalkit import metrics, report, verification
from app.evalkit.golden import GOLDEN_DIR, GoldenIndex, load_cases, load_kb
from app.evalkit.render import render_rfp
from app.evalkit.run import DEGRADE_SUFFIX, load_recordings, run_golden
from app.ingestion import pipeline
from app.ingestion.redaction import get_redaction_engine

BASELINE_PATH = GOLDEN_DIR.parent / "baseline.json"


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_pipeline.py"
    spec = importlib.util.spec_from_file_location("eval_pipeline_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline_run():
    return run_golden(mode="replay", variant="baseline")


@pytest.fixture(scope="module")
def degraded_run():
    return run_golden(mode="replay", variant="degraded")


# ── The regression gate (this is what CI enforces) ───────


class TestRegressionGate:
    def test_the_committed_baseline_is_reproduced_by_replay(self, baseline_run):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        regressions = metrics.compare(baseline_run.metrics, baseline["metrics"], baseline["tolerance"])
        assert regressions == [], "\n".join(regressions)

    def test_replay_is_deterministic(self, baseline_run):
        again = run_golden(mode="replay", variant="baseline")
        assert again.metrics == baseline_run.metrics

    def test_the_baseline_file_is_the_replayed_metrics_not_hand_edited(self, baseline_run):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["metrics"]
        assert {k: baseline_run.metrics[k] for k in baseline} == baseline

    def test_a_deliberately_degraded_prompt_lowers_at_least_two_metrics(self, baseline_run, degraded_run):
        worse = [
            name
            for name in metrics.HIGHER_IS_BETTER + metrics.LOWER_IS_BETTER
            if baseline_run.metrics[name] is not None
            and degraded_run.metrics[name] is not None
            and metrics.compare({name: degraded_run.metrics[name]}, {name: baseline_run.metrics[name]}, tolerance=0.0)
        ]
        assert len(worse) >= 2, worse
        # and the gate would catch it
        assert metrics.compare(degraded_run.metrics, baseline_run.metrics, 0.02)

    def test_the_app_still_removes_the_invented_figures_the_degraded_model_wrote(self, degraded_run):
        assert degraded_run.metrics["unsupported_claim_rate_raw"] > 0
        assert degraded_run.metrics["unsupported_claim_rate_final"] == 0

    def test_the_degrade_instruction_is_what_the_recordings_were_made_with(self):
        assert "Ignore the sources" in DEGRADE_SUFFIX

    def test_a_missing_recording_counts_as_a_failure_not_a_pass(self, tmp_path):
        golden = tmp_path / "golden"
        shutil.copytree(GOLDEN_DIR, golden, ignore=shutil.ignore_patterns("recordings"))
        run = run_golden(mode="replay", variant="baseline", golden_dir=golden, only="clinic-pdf")
        # C6 (HITRUST) has nothing in the knowledge base, so the model is never asked and needs no recording
        assert {s.id for s in run.scores if s.answered} == {"C6"}
        assert all("no usable answer" in s.problems for s in run.scores if s.id != "C6")

    def test_a_stale_recording_is_reported_per_requirement(self, tmp_path):
        golden = tmp_path / "golden"
        shutil.copytree(GOLDEN_DIR, golden)
        path = golden / "recordings" / "baseline" / "clinic-pdf.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        del recorded["C3"]
        path.write_text(json.dumps(recorded), encoding="utf-8")

        run = run_golden(mode="replay", variant="baseline", golden_dir=golden, only="clinic-pdf")

        assert [s.id for s in run.scores if not s.answered] == ["C3"]


# ── What the golden set shows ────────────────────────────


class TestBaselineFindings:
    def test_every_requirement_was_extracted_from_every_format(self, baseline_run):
        assert baseline_run.metrics["extraction_recall"] == 1.0
        assert {c.filename.rsplit(".", 1)[-1] for c in baseline_run.cases} == {"docx", "xlsx", "pdf"}

    def test_a_format_instruction_is_a_known_false_positive(self, baseline_run):
        # "Each answer shall be no longer than 200 words" is extracted as a requirement (tracked in #32)
        (case,) = [c for c in baseline_run.cases if c.id == "council-docx"]
        assert case.extra == ["shall be no longer than 200 words"]

    def test_the_current_policy_beats_the_stale_proposal_for_conflicting_facts(self, baseline_run):
        scores = {(s.case, s.id): s for s in baseline_run.scores}
        assert scores[("council-docx", "S2")].retrieval_rank == 1  # key rotation: 90 days, not "yearly"
        assert scores[("council-docx", "S2")].answer_ok

    def test_no_unsupported_requirement_is_answered_with_prose(self, baseline_run):
        # Before answer states (#33) the model prose-answered 60% of these. Now each ends as a placeholder for a person.
        unsupported = [s for s in baseline_run.scores if not s.supported]
        assert len(unsupported) >= 7
        assert baseline_run.metrics["false_answer_rate"] == 0
        assert all(s.state in states.PLACEHOLDER_STATES and s.meets == "" for s in unsupported)

    def test_pricing_and_legal_requirements_are_routed_to_people_not_drafted(self, baseline_run):
        scores = {(s.case, s.id): s for s in baseline_run.scores}
        assert scores[("retail-xlsx", "RT-09")].state == states.NEEDS_COMMERCIAL
        assert scores[("retail-xlsx", "RT-10")].state == states.NEEDS_LEGAL


# ── Scoring pieces ───────────────────────────────────────


class TestMatching:
    EXTRACTED = [
        {"text": "shall encrypt all customer data at rest and in transit"},
        {"text": "must hold FedRAMP High authorisation"},
        {"text": "shall be no longer than 200 words"},
    ]

    def test_extracted_text_matches_even_when_the_subject_was_dropped(self):
        expected = [
            {"id": "A", "text": "The vendor shall encrypt all customer data at rest and in transit."},
            {"id": "B", "text": "The vendor must hold FedRAMP High authorisation."},
        ]
        assert metrics.match_requirements(self.EXTRACTED, expected) == {"A": 0, "B": 1}

    def test_unrelated_text_does_not_match(self):
        assert metrics.match_requirements(self.EXTRACTED, [{"id": "Z", "text": "Provide a disaster recovery plan."}]) == {}

    def test_one_extracted_item_cannot_satisfy_two_expectations(self):
        expected = [{"id": "A", "text": "encrypt customer data at rest"}, {"id": "A2", "text": "encrypt customer data at rest"}]
        assert len(metrics.match_requirements(self.EXTRACTED, expected)) == 1


class TestAnswerChecks:
    @pytest.mark.parametrize(
        "answer, expected",
        [
            ({"answer_markdown": "Not provided in sources – recommend human review.", "meets": "No"}, True),
            ({"answer_markdown": "We cannot say. Please recommend human review of this.", "meets": "Partial"}, True),
            ({"answer_markdown": "Nothing found.", "meets": "No", "evidence": []}, True),
            ({"answer_markdown": "We support SSO.", "meets": "Yes", "evidence": [{"quote": "q"}]}, False),
            ({"answer_markdown": "We do not offer that.", "meets": "No", "evidence": [{"quote": "q"}]}, False),
        ],
    )
    def test_what_counts_as_declining(self, answer, expected):
        assert metrics.is_abstention(answer) is expected

    def test_a_citation_counts_only_if_its_quote_is_in_the_cited_document(self):
        docs = [{"source_url": "/kb/doc/a", "content": "Data is encrypted with AES-256 at rest."}]
        answer = {
            "evidence": [
                {"source_url": "/kb/doc/a", "quote": "encrypted with  AES-256 at rest"},  # whitespace differs: fine
                {"source_url": "/kb/doc/a", "quote": "keys are rotated every day, always"},  # not in the document
                {"source_url": "/kb/doc/b", "quote": "encrypted with AES-256 at rest"},  # a different document
                {"source_url": "/kb/doc/a", "quote": ""},  # no quote to check
            ]
        }
        assert metrics.count_citations(answer, docs) == (4, 1)

    def test_required_and_forbidden_phrases(self):
        text = "Keys rotate every 90 days. We use AES-256."
        assert metrics.check_answer_text(text, [["90 days"], ["AES-256", "AES-128"]], ["yearly"]) == []
        assert metrics.check_answer_text(text, [["30 days"]], ["AES-256"]) == [
            "missing one of: 30 days",
            "contains forbidden: AES-256",
        ]

    def test_unsupported_figures_are_those_not_in_the_sources(self):
        assert metrics.unsupported_figures("We cut cost by 40% in 14 weeks.", "It took 14 weeks.") == ["40%"]


class TestSummary:
    def score(self, **kwargs):
        defaults = dict(case="c", id="r", text="t", supported=True, extracted=True, answered=True)
        return metrics.RequirementScore(**{**defaults, **kwargs})

    def test_rates_are_computed_over_what_can_be_measured(self):
        scores = [
            self.score(id="1", retrieval_expected=True, retrieval_rank=1, abstention_ok=True, citations=2, citations_supported=2),
            self.score(id="2", retrieval_expected=True, retrieval_rank=None, abstention_ok=True, citations=2, citations_supported=1, unsupported_raw=["9%"]),
            self.score(id="3", supported=False, abstained=False, abstention_ok=False),
            self.score(id="4", answered=False),
        ]
        summary = metrics.summarise(scores, expected_total=5, extracted_total=4, matched_total=4)
        assert summary["extraction_recall"] == 0.8 and summary["extraction_precision"] == 1.0
        assert summary["retrieval_hit_rate"] == 0.5 and summary["retrieval_mrr"] == 0.5
        assert summary["answer_rate"] == 0.75
        assert summary["citation_support_rate"] == 0.75
        assert summary["unsupported_claim_rate_raw"] == pytest.approx(0.3333, abs=0.001)
        assert summary["false_answer_rate"] == 1.0
        assert summary["answer_correctness"] is None  # nothing set expected phrases

    def test_cost_needs_both_prices_and_token_counts(self):
        summary = {"input_tokens_per_requirement": 1600, "output_tokens_per_requirement": 400}
        assert metrics.estimate_cost(summary, 2.5, 10.0) == pytest.approx(0.008)
        assert metrics.estimate_cost(summary, None, 10.0) is None
        assert metrics.estimate_cost({}, 2.5, 10.0) is None


class TestCompare:
    BASE = {"extraction_recall": 0.95, "false_answer_rate": 0.2, "answer_correctness": None}

    def now(self, **changes):
        return {**self.BASE, **changes}

    def test_a_drop_within_tolerance_passes(self):
        assert metrics.compare(self.now(extraction_recall=0.94, false_answer_rate=0.21), self.BASE, 0.02) == []

    def test_a_drop_beyond_tolerance_fails(self):
        (line,) = metrics.compare(self.now(extraction_recall=0.90), self.BASE, 0.02)
        assert "extraction_recall: 0.95 -> 0.9" in line

    def test_for_lower_is_better_metrics_a_rise_is_the_regression(self):
        assert metrics.compare(self.now(false_answer_rate=0.30), self.BASE, 0.02)
        assert metrics.compare(self.now(false_answer_rate=0.10), self.BASE, 0.02) == []

    def test_an_improvement_never_fails(self):
        assert metrics.compare(self.now(extraction_recall=1.0, false_answer_rate=0.0), self.BASE, 0.0) == []

    def test_a_metric_that_stopped_being_measured_is_a_regression(self):
        assert metrics.compare(self.now(extraction_recall=None), self.BASE, 0.02)


# ── The golden set itself ────────────────────────────────


class TestGoldenFixtures:
    def test_each_format_renders_and_extracts(self):
        for case in load_cases():
            result = pipeline.extract_requirements(case.filename, render_rfp(case.spec))
            assert len(result.requirements) >= len(case.requirements), case.id

    def test_there_is_a_trap_of_each_kind(self):
        requirements = [r for c in load_cases() for r in c.requirements]
        assert any(not r["supported"] for r in requirements)  # nothing in the knowledge base
        assert any(r.get("forbid") for r in requirements)  # a stale conflicting document
        assert any(r.get("meets_in") for r in requirements)  # a claim the sources only partly support
        assert any(r["text"].rstrip().endswith("?") for r in requirements)  # questions

    def test_the_knowledge_base_has_a_current_and_a_stale_version_of_the_same_fact(self):
        kinds = {d["title"]: d["source_type"] for d in load_kb()}
        assert kinds["security-policy-2026.pdf"] == "authoritative" and kinds["security-proposal-2022.pdf"] == "historical"

    def test_the_fixtures_contain_no_real_contact_or_confidential_data(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in GOLDEN_DIR.glob("*.json")) + "\n".join(
            p.read_text(encoding="utf-8") for p in (GOLDEN_DIR / "rfps").glob("*.json")
        )
        assert not re.search(r"[\w.+-]+@[\w-]+\.\w+|https?://|\b\d{3}[-\s]\d{3}[-\s]\d{4}\b", text)
        assert get_redaction_engine().redact(text).redaction_count == 0

    def test_every_recording_belongs_to_a_known_requirement(self):
        ids = {c.id: {r["id"] for r in c.requirements} for c in load_cases()}
        for variant in ("baseline", "degraded"):
            for case_id, requirement_ids in ids.items():
                recorded = {key.removesuffix("#verify") for key in load_recordings(GOLDEN_DIR, variant, case_id)}
                assert recorded <= requirement_ids
                # Only requirements the model is never asked about (routed, or nothing in the knowledge base) lack one
                unsupported = {r["id"] for c in load_cases() if c.id == case_id for r in c.requirements if not r["supported"]}
                assert requirement_ids - recorded <= unsupported

    def test_the_fixture_index_returns_hits_shaped_like_the_search_service(self):
        index = GoldenIndex(load_kb(), "https://x/y")
        (first, *_) = index.search(search_text="keys stored in a hardware-backed key vault", top=3)
        assert first["title"] == "security-policy-2026.pdf"
        assert first["source_url"] == "https://x/y/security-policy-2026.pdf"
        assert first["effective_date"] == "2026-05-01T00:00:00Z" and first["page"] == 4
        assert first["@search.score"] > 0
        assert index.search(search_text="zzzz qqqq") == []


# ── The report and the command ───────────────────────────


class TestReports:
    def test_the_report_names_what_went_wrong(self, baseline_run):
        markdown = report.to_markdown(report.to_dict(baseline_run))
        assert "Abstention correctness" in markdown and "Injected errors caught" in markdown
        assert "clinic-pdf / C2" in markdown and "claim(s) the evidence does not support" in markdown

    def test_the_markdown_shows_the_baseline_beside_the_value(self, baseline_run, degraded_run):
        baseline = report.to_dict(baseline_run)["metrics"]
        markdown = report.to_markdown(report.to_dict(degraded_run), baseline)
        assert "| State correctness | 91.7% | 100.0% |" in markdown

    def test_cost_appears_when_prices_are_given(self, baseline_run):
        assert "cost_per_requirement_usd" in report.to_dict(baseline_run, input_price=2.5, output_price=10.0)["metrics"]
        assert "cost_per_requirement_usd" not in report.to_dict(baseline_run)["metrics"]

    def test_the_committed_baseline_report_matches_the_baseline_metrics(self, baseline_run):
        committed = (GOLDEN_DIR.parent / "baseline.md").read_text(encoding="utf-8")
        assert committed == report.to_markdown(report.to_dict(baseline_run))


class TestCommand:
    def test_the_check_passes_against_the_committed_baseline(self, tmp_path, capsys):
        script = load_script()
        assert script.main(["--check", "--report-dir", str(tmp_path)]) == 0
        assert (tmp_path / "baseline.json").exists() and (tmp_path / "baseline.md").exists()
        assert "No regression" in capsys.readouterr().out

    def test_the_check_fails_when_a_metric_regresses(self, tmp_path, monkeypatch, capsys):
        script = load_script()
        stricter = tmp_path / "baseline.json"
        stricter.write_text(
            json.dumps({"tolerance": 0.0, "metrics": {"answer_correctness": 0.99, "false_answer_rate": 0.0}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(script, "BASELINE", stricter)
        assert script.main(["--check", "--report-dir", str(tmp_path)]) == 1
        assert "REGRESSION" in capsys.readouterr().err

    def test_the_degraded_recordings_fail_the_check(self, tmp_path):
        assert load_script().main(["--variant", "degraded", "--check", "--report-dir", str(tmp_path)]) == 1

    def test_without_a_baseline_the_check_cannot_pass(self, tmp_path, monkeypatch):
        script = load_script()
        monkeypatch.setattr(script, "BASELINE", tmp_path / "missing.json")
        assert script.main(["--check", "--report-dir", str(tmp_path)]) == 2

    def test_live_only_options_are_refused_in_replay(self, tmp_path):
        script = load_script()
        for flag in ("--degrade", "--record"):
            with pytest.raises(SystemExit):
                script.main([flag, "--report-dir", str(tmp_path)])

    def test_a_baseline_can_only_be_written_from_a_full_replay(self, tmp_path):
        script = load_script()
        with pytest.raises(SystemExit):
            script.main(["--write-baseline", "--case", "clinic-pdf", "--report-dir", str(tmp_path)])

    def test_one_case_can_be_run_alone(self, tmp_path):
        assert load_script().main(["--case", "clinic-pdf", "--report-dir", str(tmp_path)]) == 0
        report_json = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
        assert {r["case"] for r in report_json["requirements"]} == {"clinic-pdf"}

# ── The claim verifier ───────────────────────────────────


class TestVerifierEvaluation:
    def test_the_verifier_meets_the_agreed_targets_on_the_injected_claims(self, baseline_run):
        m = baseline_run.metrics
        assert m["injection_recall"] >= verification.TARGET_RECALL
        assert m["injection_false_positive_rate"] <= verification.TARGET_FALSE_POSITIVE_RATE

    def test_the_injected_set_is_a_real_test(self, baseline_run):
        outcomes = baseline_run.injections
        wrong = [o for o in outcomes if o.expect == "wrong"]
        right = [o for o in outcomes if o.expect == "right"]
        assert len(wrong) >= 15 and len(right) >= 15
        # every verdict came from a rule or from a recorded verifier reply: none is "no recording"
        assert all(o.verdict for o in outcomes)
        assert {o.verdict for o in right} == {"supported"}
        assert {o.verdict for o in wrong} >= {"unsupported", "contradicted", "partial"}

    def test_fabricated_quotes_uncited_claims_and_wrong_figures_are_caught_with_no_model_call(self, baseline_run):
        rules = [o for o in baseline_run.injections if o.kind in verification.RULE_KINDS]
        assert len(rules) >= 4
        assert all(o.ok and o.checked_by == "rule" and o.model_calls == 0 for o in rules)
        assert baseline_run.metrics["deterministic_catch_rate"] == 1.0

    def test_the_verifiers_cost_and_latency_are_reported(self, baseline_run):
        m = baseline_run.metrics
        assert m["verifier_calls"] > 0 and m["verification_latency_mean_s"] > 0
        assert m["verification_input_tokens_per_call"] > 0 and m["verification_output_tokens_per_call"] > 0
        markdown = report.to_markdown(report.to_dict(baseline_run, input_price=2.5, output_price=10.0))
        assert "Verification latency per requirement" in markdown and "Estimated verification cost" in markdown

    def test_the_writers_own_claims_on_the_golden_rfps_are_checked_too(self, baseline_run):
        flagged = [s for s in baseline_run.scores if s.verification == "flagged"]
        assert flagged and all("the evidence does not support" in " ".join(s.problems) for s in flagged)
        assert baseline_run.metrics["claim_support_rate"] < 1.0  # an honest baseline: the writer overreaches sometimes

    def test_a_verifier_that_approves_everything_is_noticed(self, tmp_path):
        # Same claims, but the recorded verifier says "supported" to all of them: recall must collapse
        golden = tmp_path / "golden"
        shutil.copytree(GOLDEN_DIR, golden)
        path = golden / "recordings" / "baseline" / "injections.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        for record in recorded.values():
            claim_ids = re.findall(r'"id":\s*"(C\d+)"', record["text"]) or ["C1"]
            record["text"] = json.dumps({"verdicts": [{"id": i, "verdict": "supported", "reason": "ok"} for i in claim_ids]})
        path.write_text(json.dumps(recorded), encoding="utf-8")

        lax = run_golden(mode="replay", variant="baseline", golden_dir=golden)

        assert lax.metrics["injection_recall"] < verification.TARGET_RECALL
        assert lax.metrics["deterministic_catch_rate"] == 1.0  # the rules do not depend on the model
        assert metrics.compare(lax.metrics, json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["metrics"], 0.02)

    def test_a_verifier_that_rejects_everything_is_noticed_too(self, tmp_path):
        golden = tmp_path / "golden"
        shutil.copytree(GOLDEN_DIR, golden)
        path = golden / "recordings" / "baseline" / "injections.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        for record in recorded.values():
            record["text"] = json.dumps({"verdicts": [{"id": "C1", "verdict": "unsupported", "reason": "no"}]})
        path.write_text(json.dumps(recorded), encoding="utf-8")

        strict = run_golden(mode="replay", variant="baseline", golden_dir=golden)

        assert strict.metrics["injection_false_positive_rate"] > verification.TARGET_FALSE_POSITIVE_RATE

    def test_a_missing_recording_leaves_the_claim_unchecked_not_accepted(self, tmp_path):
        golden = tmp_path / "golden"
        shutil.copytree(GOLDEN_DIR, golden)
        (golden / "recordings" / "baseline" / "injections.json").write_text("{}", encoding="utf-8")
        run = run_golden(mode="replay", variant="baseline", golden_dir=golden)
        right = [o for o in run.injections if o.expect == "right"]
        assert all(o.verdict is None and not o.ok for o in right)
        assert run.metrics["injection_false_positive_rate"] == 1.0

    def test_every_injected_case_is_well_formed(self):
        cases = verification.load_injections(GOLDEN_DIR)
        assert len({c["id"] for c in cases}) == len(cases)
        docs = {d["title"]: " ".join(c["text"] for c in d["chunks"]) for d in load_kb()}
        for case in cases:
            assert case["expect"] in ("wrong", "right") and case["claim"] and case["requirement"]
            for item in case.get("evidence", []):
                assert item["doc"] in docs
                if case["expect"] == "right":  # a correct claim cites a real quote
                    assert " ".join(item["quote"].split()).lower() in " ".join(docs[item["doc"]].split()).lower()
