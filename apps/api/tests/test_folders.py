"""Filing files.

The interesting half of a folder tree is what it refuses: a cycle, a hierarchy
deeper than anyone can read, two siblings with the same name, and — the decision
this feature turns on — deleting a folder that still holds work.
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Document, DocumentVersion, Folder
from app.services.artifacts import folders


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    yield identity
    db = SessionLocal()
    try:
        for model in (DocumentVersion, Document, Folder):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def make_folder(client, name: str, parent_id: str = "") -> str:
    response = client.post(
        "/api/folders", json={"name": name, "parent_id": parent_id}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def make_document(client, title: str, folder_id: str = "") -> str:
    response = client.post(
        "/api/documents",
        json={"title": title, "content": "body", "folder_id": folder_id},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --------------------------------------------------------------------------
# The shape of the tree


def test_a_folder_is_created_at_the_top_level_and_listed(client, workspace):
    folder_id = make_folder(client, "  Research  ")
    rows = client.get("/api/folders").json()
    assert [(row["id"], row["name"], row["parent_id"]) for row in rows] == [
        (folder_id, "Research", "")
    ]


def test_folders_nest_and_report_their_parent(client, workspace):
    parent = make_folder(client, "Research")
    child = make_folder(client, "Interviews", parent)
    rows = {row["id"]: row["parent_id"] for row in client.get("/api/folders").json()}
    assert rows[child] == parent


def test_two_siblings_cannot_share_a_name_however_it_is_cased(client, workspace):
    make_folder(client, "Research")
    clash = client.post("/api/folders", json={"name": "  research", "parent_id": ""})
    assert clash.status_code == 422
    assert "already exists" in clash.json()["detail"]
    # The same name under a different parent is fine: the whole point of a
    # hierarchy is that "2024/Notes" and "2025/Notes" are different places.
    other = make_folder(client, "Archive")
    assert client.post(
        "/api/folders", json={"name": "Research", "parent_id": other}
    ).status_code == 201


def test_a_folder_needs_a_name_that_is_one_line(client, workspace):
    assert client.post("/api/folders", json={"name": "   "}).status_code == 422
    two_lines = client.post("/api/folders", json={"name": "Research\nNotes"})
    assert two_lines.status_code == 422
    assert "single line" in two_lines.json()["detail"]


def test_an_unknown_parent_is_refused_rather_than_orphaning_the_folder(
    client, workspace
):
    refused = client.post("/api/folders", json={"name": "Orphan", "parent_id": "nope"})
    assert refused.status_code == 422
    assert client.get("/api/folders").json() == []


def test_the_tree_stops_at_the_depth_limit(client, workspace):
    parent = ""
    for level in range(folders.MAX_DEPTH):
        parent = make_folder(client, f"Level {level}", parent)
    too_deep = client.post(
        "/api/folders", json={"name": "One too many", "parent_id": parent}
    )
    assert too_deep.status_code == 422
    assert str(folders.MAX_DEPTH) in too_deep.json()["detail"]


# --------------------------------------------------------------------------
# Renaming and moving


def test_a_folder_is_renamed_in_place(client, workspace):
    folder_id = make_folder(client, "Reserch")
    renamed = client.patch(f"/api/folders/{folder_id}", json={"name": "Research"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Research"
    assert renamed.json()["parent_id"] == ""


def test_a_rename_that_collides_with_a_sibling_is_refused(client, workspace):
    make_folder(client, "Research")
    other = make_folder(client, "Archive")
    clash = client.patch(f"/api/folders/{other}", json={"name": "Research"})
    assert clash.status_code == 422
    # And renaming a folder to the name it already has is not a collision with
    # itself, which is the off-by-one this check invites.
    assert client.patch(
        f"/api/folders/{other}", json={"name": "Archive"}
    ).status_code == 200


def test_a_folder_moves_between_parents_and_back_to_the_top(client, workspace):
    home = make_folder(client, "Research")
    guest = make_folder(client, "Interviews")
    moved = client.patch(f"/api/folders/{guest}", json={"parent_id": home})
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == home
    # An explicit empty parent means the top level, and is a different request
    # from omitting the field.
    back = client.patch(f"/api/folders/{guest}", json={"parent_id": ""})
    assert back.json()["parent_id"] == ""


def test_omitting_a_field_leaves_it_alone(client, workspace):
    home = make_folder(client, "Research")
    child = make_folder(client, "Interviews", home)
    renamed = client.patch(f"/api/folders/{child}", json={"name": "Transcripts"})
    assert renamed.json() == {
        **renamed.json(),
        "name": "Transcripts",
        "parent_id": home,
    }


def test_a_folder_cannot_be_moved_into_itself_or_its_own_descendant(client, workspace):
    root = make_folder(client, "Research")
    child = make_folder(client, "Interviews", root)
    grandchild = make_folder(client, "2025", child)

    into_self = client.patch(f"/api/folders/{root}", json={"parent_id": root})
    assert into_self.status_code == 422
    assert "itself" in into_self.json()["detail"]

    into_descendant = client.patch(f"/api/folders/{root}", json={"parent_id": grandchild})
    assert into_descendant.status_code == 422
    assert "inside itself" in into_descendant.json()["detail"]

    # The tree is unchanged: a refused move must not half-apply.
    rows = {row["id"]: row["parent_id"] for row in client.get("/api/folders").json()}
    assert rows == {root: "", child: root, grandchild: child}


def test_a_move_that_would_push_a_subtree_past_the_limit_is_refused(client, workspace):
    # A chain deep enough that anything with children cannot sit at its foot.
    parent = ""
    chain = []
    for level in range(folders.MAX_DEPTH - 1):
        parent = make_folder(client, f"Level {level}", parent)
        chain.append(parent)

    tall_root = make_folder(client, "Tall")
    make_folder(client, "Tall child", tall_root)

    refused = client.patch(f"/api/folders/{tall_root}", json={"parent_id": chain[-1]})
    assert refused.status_code == 422
    assert "levels deep" in refused.json()["detail"]

    # The same slot accepts a folder with nothing under it, so the refusal was
    # about the subtree's height and not about the destination.
    flat = make_folder(client, "Flat")
    assert client.patch(
        f"/api/folders/{flat}", json={"parent_id": chain[-1]}
    ).status_code == 200


# --------------------------------------------------------------------------
# Filing documents


def test_a_document_is_created_in_a_folder_and_moved_out_of_it(client, workspace):
    folder_id = make_folder(client, "Research")
    document_id = make_document(client, "Interview notes", folder_id)
    assert client.get(f"/api/documents/{document_id}").json()["folder_id"] == folder_id
    listed = client.get("/api/documents").json()
    assert [row["folder_id"] for row in listed] == [folder_id]

    moved = client.put(
        f"/api/documents/{document_id}/folder", json={"folder_id": ""}
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == ""


def test_a_document_created_without_a_folder_sits_at_the_top_level(client, workspace):
    document_id = make_document(client, "Loose note")
    assert client.get(f"/api/documents/{document_id}").json()["folder_id"] == ""


def test_filing_into_a_folder_that_does_not_exist_is_refused(client, workspace):
    document_id = make_document(client, "Loose note")
    refused = client.put(
        f"/api/documents/{document_id}/folder", json={"folder_id": "nope"}
    )
    assert refused.status_code == 404
    assert client.get(f"/api/documents/{document_id}").json()["folder_id"] == ""


def test_creating_a_document_in_a_folder_that_does_not_exist_is_refused(
    client, workspace
):
    refused = client.post(
        "/api/documents",
        json={"title": "Nowhere", "content": "", "folder_id": "nope"},
    )
    assert refused.status_code == 422
    assert client.get("/api/documents").json() == []


# --------------------------------------------------------------------------
# Deletion — the decided answer


def test_an_empty_folder_is_deleted(client, workspace):
    folder_id = make_folder(client, "Research")
    assert client.delete(f"/api/folders/{folder_id}").status_code == 204
    assert client.get("/api/folders").json() == []


def test_deleting_a_folder_takes_its_empty_descendants_with_it(client, workspace):
    root = make_folder(client, "Research")
    child = make_folder(client, "Interviews", root)
    make_folder(client, "2025", child)
    keep = make_folder(client, "Archive")

    assert client.delete(f"/api/folders/{root}").status_code == 204
    assert [row["id"] for row in client.get("/api/folders").json()] == [keep]


def test_a_folder_holding_a_file_refuses_to_delete_and_says_what_is_in_it(
    client, workspace
):
    folder_id = make_folder(client, "Research")
    document_id = make_document(client, "Interview notes", folder_id)

    refused = client.delete(f"/api/folders/{folder_id}")
    # 409, not 422: nothing about the request is malformed, and it will succeed
    # unchanged once the folder is empty.
    assert refused.status_code == 409
    assert "still holds 1 file" in refused.json()["detail"]
    # Nothing was destroyed on the way to the refusal.
    assert client.get(f"/api/documents/{document_id}").status_code == 200
    assert len(client.get("/api/folders").json()) == 1

    # And the way through is the one the message describes.
    client.put(f"/api/documents/{document_id}/folder", json={"folder_id": ""})
    assert client.delete(f"/api/folders/{folder_id}").status_code == 204
    assert client.get(f"/api/documents/{document_id}").status_code == 200


def test_a_file_buried_in_a_subfolder_still_blocks_the_delete(client, workspace):
    """The check is over the whole subtree, not the folder's own children.

    Deleting the root would otherwise cascade to the descendants — which the
    empty-tree rule permits — and take a document nobody was asked about.
    """
    root = make_folder(client, "Research")
    child = make_folder(client, "Interviews", root)
    make_document(client, "Buried note", child)

    refused = client.delete(f"/api/folders/{root}")
    assert refused.status_code == 409
    assert "still holds 1 file" in refused.json()["detail"]
    assert len(client.get("/api/folders").json()) == 2


def test_deleting_a_document_leaves_its_folder_standing(client, workspace):
    folder_id = make_folder(client, "Research")
    document_id = make_document(client, "Interview notes", folder_id)
    assert client.delete(f"/api/documents/{document_id}").status_code == 204
    assert [row["id"] for row in client.get("/api/folders").json()] == [folder_id]


def test_an_unknown_folder_is_a_404_on_every_route_that_names_one(client, workspace):
    assert client.patch("/api/folders/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/folders/nope").status_code == 404
