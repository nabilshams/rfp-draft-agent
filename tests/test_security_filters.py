"""Tests for security filter (DLAC + segmentation) query building."""


from app.agents.retrieval import build_odata_filter, build_search_options


class TestODataFilter:
    def test_basic_filter_includes_client_and_project(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert "client_id eq 'acme'" in f
        assert "project_id eq 'proj-42'" in f

    def test_filter_includes_solution_area(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            solution_area="cloud",
            user_oid="user-123",
        )
        assert "solution_area eq 'cloud'" in f

    def test_filter_omits_solution_area_when_none(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert "solution_area" not in f

    def test_filter_includes_oid_dlac(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-abc-123",
        )
        assert "oids/any(o: o eq 'user-abc-123')" in f

    def test_filter_includes_group_dlac(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
            user_groups=["grp-a", "grp-b"],
        )
        assert "groups/any(g: search.in(g, 'grp-a,grp-b'))" in f

    def test_filter_combines_oid_and_groups_with_or(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
            user_groups=["grp-x"],
        )
        # The DLAC clause must use OR between oid and groups
        assert " or " in f

    def test_filter_escapes_single_quotes(self):
        f = build_odata_filter(
            client_id="o'reilly",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert "o''reilly" in f  # OData single-quote escape

    def test_segmentation_and_dlac_are_anded(self):
        f = build_odata_filter(
            client_id="acme",
            project_id="proj-42",
            solution_area="cloud",
            user_oid="user-123",
            user_groups=["grp-1"],
        )
        # All top-level clauses joined by " and "
        parts = f.split(" and ")
        assert len(parts) >= 4  # client, project, solution_area, dlac


class TestSearchOptions:
    def test_options_include_filter(self):
        opts = build_search_options(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert "filter" in opts
        assert "client_id eq 'acme'" in opts["filter"]

    def test_options_have_top(self):
        opts = build_search_options(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
            top=5,
        )
        assert opts["top"] == 5

    def test_options_select_fields(self):
        opts = build_search_options(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert "content" in opts["select"]
        assert "source_url" in opts["select"]
        assert "title" in opts["select"]

    def test_options_use_semantic_query_type(self):
        opts = build_search_options(
            client_id="acme",
            project_id="proj-42",
            user_oid="user-123",
        )
        assert opts["query_type"] == "semantic"
