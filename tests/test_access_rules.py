"""Unit tests for the ownership rules, group sharing rules and the per-user rate limiter."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import access, ratelimit
from app.auth.entra import UserClaims
from app.config import Settings


def make_settings(mode="easyauth", **overrides) -> Settings:
    return Settings(
        _env_file=None,
        auth_mode=mode,
        foundry_project_endpoint="https://example.services.ai.azure.com",
        azure_ai_search_connection_name="search",
        azure_storage_account_url="https://example.blob.core.windows.net",
        **overrides,
    )


def user(oid="user-a", *groups) -> UserClaims:
    return UserClaims(oid=oid, groups=list(groups))


SETTINGS = make_settings(admin_oids="admin-1, admin-2 ,")


class TestAdmins:
    def test_admin_oids_are_parsed_leniently(self):
        assert SETTINGS.admin_oid_set == {"admin-1", "admin-2"}
        assert make_settings().admin_oid_set == frozenset()

    def test_listed_users_are_admins_and_others_are_not(self):
        assert access.is_admin(user("admin-1"), SETTINGS)
        assert not access.is_admin(user("user-a"), SETTINGS)

    def test_everyone_is_an_admin_when_authentication_is_disabled(self):
        assert access.is_admin(user("anyone"), make_settings("disabled"))

    def test_require_admin_raises_403_for_others(self):
        access.require_admin(user("admin-1"), SETTINGS)
        with pytest.raises(HTTPException) as exc:
            access.require_admin(user("user-a"), SETTINGS)
        assert exc.value.status_code == 403


class TestCanManage:
    def test_owner_and_admin_can_manage(self):
        assert access.can_manage("user-a", user("user-a"), SETTINGS)
        assert access.can_manage("user-a", user("admin-1"), SETTINGS)

    def test_other_users_cannot(self):
        assert not access.can_manage("user-a", user("user-b"), SETTINGS)

    def test_a_document_without_an_owner_is_admin_only(self):
        assert not access.can_manage(None, user("user-a"), SETTINGS)
        assert not access.can_manage("", user(""), SETTINGS)
        assert access.can_manage(None, user("admin-1"), SETTINGS)


class TestKbAcl:
    def test_metadata_round_trip(self):
        metadata = access.kb_metadata("user-a", ["g1", "g2"])
        assert metadata == {"owner_oid": "user-a", "shared_groups": "g1,g2"}
        assert access.parse_kb_acl(metadata) == access.KbAcl("user-a", ("g1", "g2"))

    def test_private_document_has_no_groups(self):
        assert access.parse_kb_acl(access.kb_metadata("user-a", [])) == access.KbAcl("user-a", ())

    def test_azure_returns_an_empty_metadata_value_as_none(self):
        # Seen on a real storage account: listing blobs gave None for shared_groups of an unshared document.
        assert access.parse_kb_acl({"owner_oid": "user-a", "shared_groups": None}) == access.KbAcl("user-a", ())
        assert access.parse_kb_acl({"owner_oid": None, "shared_groups": None}) is None

    @pytest.mark.parametrize("metadata", [None, {}, {"shared_groups": "g1"}, {"owner_oid": ""}])
    def test_documents_without_an_owner_have_no_acl(self, metadata):
        assert access.parse_kb_acl(metadata) is None

    def test_visibility(self):
        acl = access.KbAcl("user-a", ("team-1",))
        assert access.can_view_kb(acl, user("user-a"), SETTINGS)  # owner
        assert access.can_view_kb(acl, user("user-b", "team-1", "other"), SETTINGS)  # shared group
        assert access.can_view_kb(acl, user("admin-1"), SETTINGS)  # admin
        assert not access.can_view_kb(acl, user("user-c", "team-9"), SETTINGS)
        assert not access.can_view_kb(access.KbAcl("user-a"), user("user-b", "team-1"), SETTINGS)  # private

    def test_ownerless_documents_are_visible_to_admins_only(self):
        assert access.can_view_kb(None, user("admin-1"), SETTINGS)
        assert not access.can_view_kb(None, user("user-a"), SETTINGS)


class TestSharing:
    def test_parse_group_list(self):
        assert access.parse_group_list(" g1, g2 ,,g1 ,g3") == ["g1", "g2", "g3"]
        assert access.parse_group_list("") == []

    def test_users_can_share_with_groups_they_belong_to(self):
        assert access.resolve_share_groups(["g1"], user("user-a", "g1", "g2"), SETTINGS) == ["g1"]
        assert access.resolve_share_groups([], user("user-a"), SETTINGS) == []

    def test_users_cannot_share_with_other_groups(self):
        with pytest.raises(HTTPException) as exc:
            access.resolve_share_groups(["g1", "not-mine"], user("user-a", "g1"), SETTINGS)
        assert exc.value.status_code == 403

    def test_admins_can_share_with_any_group(self):
        assert access.resolve_share_groups(["anything"], user("admin-1"), SETTINGS) == ["anything"]

    @pytest.mark.parametrize("bad", ["a b", "a,b", "x'y", "", "g" * 65, "a/b", "g1\n"])
    def test_group_ids_must_be_plain_tokens(self, bad):
        with pytest.raises(HTTPException) as exc:
            access.resolve_share_groups([bad], user("admin-1"), SETTINGS)
        assert exc.value.status_code == 400


class FakeRfpContainer:
    def __init__(self, blobs):
        self.blobs = blobs
        self.calls = []

    def list_blobs(self, name_starts_with=None, include=None):
        self.calls.append((name_starts_with, include))
        return [b for b in self.blobs if b.name.startswith(name_starts_with)]


class TestRfpAccess:
    def container(self, owner="user-a"):
        metadata = {"owner_oid": owner} if owner else None
        return FakeRfpContainer([SimpleNamespace(name="rfp1/doc.docx", metadata=metadata)])

    def test_owner_gets_the_blob(self):
        container = self.container()
        blob = access.require_rfp_access(container, "rfp1", user("user-a"), SETTINGS)
        assert blob.name == "rfp1/doc.docx"
        assert container.calls == [("rfp1/", ["metadata"])]

    def test_admin_gets_any_blob(self):
        assert access.require_rfp_access(self.container(), "rfp1", user("admin-1"), SETTINGS)

    @pytest.mark.parametrize("who", ["user-b", ""])
    def test_other_users_get_404_so_existence_is_not_revealed(self, who):
        with pytest.raises(HTTPException) as exc:
            access.require_rfp_access(self.container(), "rfp1", user(who), SETTINGS)
        assert exc.value.status_code == 404

    def test_unknown_rfp_is_404(self):
        with pytest.raises(HTTPException) as exc:
            access.require_rfp_access(self.container(), "other", user("user-a"), SETTINGS)
        assert exc.value.status_code == 404

    def test_an_rfp_without_an_owner_is_admin_only(self):
        legacy = self.container(owner=None)
        with pytest.raises(HTTPException):
            access.require_rfp_access(legacy, "rfp1", user("user-a"), SETTINGS)
        assert access.require_rfp_access(legacy, "rfp1", user("admin-1"), SETTINGS)


class TestRateLimiter:
    @pytest.fixture
    def clock(self, monkeypatch):
        state = {"now": 1000.0}
        monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: state["now"]))
        return state

    def test_allows_up_to_the_limit_then_rejects_with_retry_after(self, clock):
        limiter = ratelimit.RateLimiter()
        for _ in range(3):
            limiter.check("user-a", 3)

        clock["now"] += 20
        with pytest.raises(HTTPException) as exc:
            limiter.check("user-a", 3)

        assert exc.value.status_code == 429
        assert exc.value.headers["Retry-After"] == "40"

    def test_the_window_slides(self, clock):
        limiter = ratelimit.RateLimiter()
        limiter.check("user-a", 1)
        clock["now"] += 61
        limiter.check("user-a", 1)  # the first request has left the window

    def test_users_are_limited_independently(self, clock):
        limiter = ratelimit.RateLimiter()
        limiter.check("user-a", 1)
        limiter.check("user-b", 1)
        with pytest.raises(HTTPException):
            limiter.check("user-a", 1)

    def test_zero_disables_the_limit(self, clock):
        limiter = ratelimit.RateLimiter()
        for _ in range(100):
            limiter.check("user-a", 0)

    def test_rejected_requests_do_not_extend_the_block(self, clock):
        limiter = ratelimit.RateLimiter()
        limiter.check("user-a", 1)
        for _ in range(5):
            clock["now"] += 5
            with pytest.raises(HTTPException):
                limiter.check("user-a", 1)
        clock["now"] += 40  # 65s after the only recorded request
        limiter.check("user-a", 1)
