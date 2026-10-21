"""Tests for the retrieval evaluation metrics and the case file format."""

import pytest

from app import evaluation
from app.evaluation import EvalCase, evaluate, format_report, load_cases


def doc(title, url=None):
    return {"title": title, "source_url": url or f"https://kb/{title}.pdf"}


def case(case_id, *expected, query="q"):
    return EvalCase(case_id, query, tuple(expected))


def fake_search(results_by_case):
    return lambda c: results_by_case.get(c.id, [])


class TestMetrics:
    def test_a_perfect_run(self):
        cases = [case("a", "alpha"), case("b", "beta")]
        report = evaluate(cases, fake_search({"a": [doc("alpha")], "b": [doc("beta")]}))
        assert (report.hit_rate, report.recall, report.mrr) == (1.0, 1.0, 1.0)

    def test_rank_decides_the_reciprocal_rank(self):
        report = evaluate([case("a", "alpha")], fake_search({"a": [doc("x"), doc("y"), doc("alpha")]}))
        assert report.results[0].first_rank == 3
        assert report.mrr == pytest.approx(1 / 3)

    def test_misses_count_as_zero(self):
        report = evaluate([case("a", "alpha"), case("b", "beta")], fake_search({"a": [doc("alpha")], "b": [doc("other")]}))
        assert report.hit_rate == 0.5
        assert report.recall == 0.5
        assert report.mrr == 0.5

    def test_recall_is_the_share_of_expected_sources_found(self):
        report = evaluate([case("a", "alpha", "beta", "gamma", "delta")], fake_search({"a": [doc("alpha"), doc("beta")]}))
        assert report.recall == 0.5
        assert report.hit_rate == 1.0  # at least one found
        assert report.results[0].found == ["alpha", "beta"]

    def test_only_the_top_k_results_count(self):
        results = {"a": [doc("x"), doc("y"), doc("alpha")]}
        assert evaluate([case("a", "alpha")], fake_search(results), k=2).hit_rate == 0.0
        assert evaluate([case("a", "alpha")], fake_search(results), k=3).hit_rate == 1.0

    def test_sources_match_the_title_or_the_url_ignoring_case(self):
        by_url = evaluate([case("a", "ENCRYPTION")], fake_search({"a": [doc("Guide", "https://kb/Encryption-Guide.pdf")]}))
        by_title = evaluate([case("a", "encryption")], fake_search({"a": [doc("Data Encryption Guide")]}))
        assert by_url.hit_rate == by_title.hit_rate == 1.0

    def test_the_best_rank_across_expected_sources_is_used(self):
        report = evaluate([case("a", "beta", "alpha")], fake_search({"a": [doc("alpha"), doc("beta")]}))
        assert report.results[0].first_rank == 1

    def test_no_cases_gives_zeros_instead_of_an_error(self):
        report = evaluate([], fake_search({}))
        assert (report.hit_rate, report.recall, report.mrr) == (0.0, 0.0, 0.0)


class TestCaseFile:
    def write(self, tmp_path, text):
        path = tmp_path / "cases.jsonl"
        path.write_text(text, encoding="utf-8")
        return path

    def test_cases_are_read_and_comments_and_blank_lines_skipped(self, tmp_path):
        path = self.write(
            tmp_path,
            '# a comment\n\n{"id": "one", "query": "q1", "expected_sources": ["a", "b"]}\n'
            '{"query": "q2", "expected_sources": ["c"]}\n',
        )

        cases = load_cases(path)

        assert cases == [EvalCase("one", "q1", ("a", "b")), EvalCase("case-2", "q2", ("c",))]

    @pytest.mark.parametrize(
        "line",
        ["not json", '{"query": "q"}', '{"expected_sources": ["a"]}', '{"query": "q", "expected_sources": []}',
         '{"query": "  ", "expected_sources": ["a"]}', '{"query": "q", "expected_sources": "a"}', "[1, 2]"],
    )
    def test_malformed_lines_are_reported_with_their_line_number(self, tmp_path, line):
        path = self.write(tmp_path, f'# header\n{line}\n')
        with pytest.raises(ValueError, match=r"cases\.jsonl:2"):
            load_cases(path)

    def test_the_shipped_example_is_valid(self):
        from pathlib import Path

        example = Path(__file__).resolve().parents[1] / "evals" / "retrieval_cases.example.jsonl"
        assert len(load_cases(example)) >= 3


def test_report_compares_modes_and_lists_the_misses():
    cases = [case("a", "alpha", query="find alpha"), case("b", "beta", query="find beta")]
    keyword = evaluate(cases, fake_search({"a": [doc("alpha")]}))
    hybrid = evaluate(cases, fake_search({"a": [doc("alpha")], "b": [doc("beta")]}))

    text = format_report({"keyword": keyword, "hybrid": hybrid})

    lines = text.splitlines()
    assert lines[0].split() == ["mode", "cases", "hit", "rate", "recall@k", "MRR"]
    assert lines[1].split() == ["keyword", "2", "0.50", "0.50", "0.50"]
    assert lines[2].split() == ["hybrid", "2", "1.00", "1.00", "1.00"]
    assert "[keyword] no expected source in the top 5 for b: find beta" in text
    assert "[hybrid]" not in text.split("\n\n", 1)[-1]
    assert evaluation.format_report({"m": hybrid}, show_misses=False).count("\n") == 1
