"""A real-storage lesson: a document uploaded and then listed, with the metadata Azure actually returns."""

from .test_authorization_api import ADMIN, A, B, upload_kb


async def test_an_unshared_document_can_be_listed_straight_after_upload(client, world):
    assert (await upload_kb(client, A)).status_code == 200

    listing = await client.get("/kb/list", headers=A)

    assert listing.status_code == 200, listing.text
    (doc,) = listing.json()["documents"]
    assert doc["owned_by_me"] is True and doc["can_delete"] is True and doc["shared_with_groups"] == []


async def test_listing_still_hides_it_from_other_users_and_shows_it_to_admins(client, world):
    await upload_kb(client, A)
    assert (await client.get("/kb/list", headers=B)).json()["count"] == 0
    assert (await client.get("/kb/list", headers=ADMIN)).json()["count"] == 1


async def test_a_shared_document_lists_its_groups(client, world):
    await upload_kb(client, A, share="team-1")
    (doc,) = (await client.get("/kb/list", headers=A)).json()["documents"]
    assert doc["shared_with_groups"] == ["team-1"]
