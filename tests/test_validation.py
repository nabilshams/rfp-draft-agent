"""Tests for the validators that guard blob names, file names and search filter values."""

import pytest

from app import validation


class TestSegments:
    @pytest.mark.parametrize("value", ["demo-client", "general", "Acme Corp", "client_1", "a", "v1.2", "A" * 64])
    def test_accepts_ordinary_identifiers(self, value):
        assert validation.check_segment(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", " leading", "trailing ", "a/b", "a\\b", "..", ".hidden", "-dash", "it's", 'say"hi"', "a\nb", "a\n", "a\x00b", "A" * 65],
    )
    def test_rejects_anything_that_could_alter_a_path_or_filter(self, value):
        with pytest.raises(ValueError):
            validation.check_segment(value)


class TestRfpIds:
    @pytest.mark.parametrize("value", ["abc123", "3f2a9c1d-7b", "A" * 64])
    def test_accepts_generated_ids(self, value):
        assert validation.check_rfp_id(value) == value

    @pytest.mark.parametrize("value", ["", "a b", "a/b", "..", "a" * 65, "id\n"])
    def test_rejects_other_values(self, value):
        with pytest.raises(ValueError):
            validation.check_rfp_id(value)


class TestFilenames:
    @pytest.mark.parametrize("value", ["rfp.docx", "My RFP (final).pdf", "o'brien.pdf", "résumé.pdf", "a" * 200])
    def test_accepts_plain_names(self, value):
        assert validation.check_filename(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", ".", "..", "../x.pdf", "a/b.pdf", "a\\b.pdf", " x.pdf", "x.pdf ", "a\tb.pdf", "a\x00.pdf", "a\x7f.pdf", "a" * 201],
    )
    def test_rejects_paths_and_control_characters(self, value):
        with pytest.raises(ValueError):
            validation.check_filename(value)


class TestKbBlobNames:
    def test_accepts_the_four_part_layout(self):
        name = "demo-client/demo-project/general/guide.pdf"
        assert validation.check_kb_blob_name(name) == name

    @pytest.mark.parametrize(
        "value",
        [
            "guide.pdf",
            "a/b/c",
            "a/b/c/d/e.pdf",
            "../b/c/d.pdf",
            "a/b/c/../d.pdf",
            "a//c/d.pdf",
            "a/b/c/",
            "a/b/c/x\\y.pdf",
        ],
    )
    def test_rejects_other_shapes(self, value):
        with pytest.raises(ValueError):
            validation.check_kb_blob_name(value)


def test_only_the_generated_output_names_are_allowed():
    assert validation.OUTPUT_FILENAMES == {"draft.docx", "compliance.xlsx"}
