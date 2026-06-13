import json
import sys
import types
from pathlib import Path


def _make_board(client, name="Test"):
    return client.post("/api/boards", json={"name": name}).json()


def test_create_node_assigns_short_id(client):
    b = _make_board(client)
    r = client.post(
        "/api/nodes",
        json={"board_id": b["id"], "type": "image", "x": 10, "y": 20},
    )
    assert r.status_code == 200
    node = r.json()
    assert node["board_id"] == b["id"]
    assert node["type"] == "image"
    assert node["x"] == 10 and node["y"] == 20
    assert len(node["short_id"]) == 4
    assert node["status"] == "idle"


def test_short_ids_unique_within_board(client):
    b = _make_board(client)
    ids = set()
    for _ in range(50):
        n = client.post(
            "/api/nodes", json={"board_id": b["id"], "type": "note"}
        ).json()
        assert n["short_id"] not in ids
        ids.add(n["short_id"])


def test_patch_node_partial(client):
    b = _make_board(client)
    n = client.post(
        "/api/nodes",
        json={"board_id": b["id"], "type": "image", "x": 0, "y": 0},
    ).json()

    r = client.patch(f"/api/nodes/{n['id']}", json={"x": 123.5, "status": "running"})
    assert r.status_code == 200
    out = r.json()
    assert out["x"] == 123.5
    assert out["status"] == "running"
    assert out["y"] == 0  # unchanged


def test_patch_missing_node_returns_404(client):
    r = client.patch("/api/nodes/999", json={"x": 1})
    assert r.status_code == 404


# ── data-merge regression tests ────────────────────────────────────────────
#
# The PATCH route used to wholesale-replace `node.data`. Any frontend caller
# that built a fresh `data` object without listing every existing field
# silently erased the missing ones. The most visible casualty was
# `aspectRatio`: every image gen wrote it, then the auto-brief vision
# patch a few seconds later replaced `data` without listing aspectRatio,
# wiping it from DB across ~50 nodes before anyone noticed.
#
# These tests pin the new merge semantic so the regression can never
# come back: PATCH `data` is a partial merge, `null` values delete keys,
# and missing keys preserve existing values verbatim.


def _make_image_node(client) -> dict:
    b = _make_board(client)
    return client.post(
        "/api/nodes",
        json={
            "board_id": b["id"],
            "type": "image",
            "data": {
                "title": "Hero",
                "prompt": "studio shot",
                "mediaId": "abc",
                "aspectRatio": "IMAGE_ASPECT_RATIO_PORTRAIT",
                "aiBrief": "young woman in cream blouse",
                "variantCount": 4,
            },
        },
    ).json()


def test_patch_data_merge_preserves_untouched_fields(client):
    """Patching `data` with a subset of keys MUST keep every other key
    intact. This was the root cause of the aspectRatio data-loss bug."""
    n = _make_image_node(client)

    # Simulate auto-brief style update — only aiBrief is in the patch.
    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"data": {"aiBrief": "updated brief from vision"}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # Patched key took the new value.
    assert data["aiBrief"] == "updated brief from vision"
    # Every untouched key kept its original value — this is the
    # invariant the old wholesale-replace broke.
    assert data["title"] == "Hero"
    assert data["prompt"] == "studio shot"
    assert data["mediaId"] == "abc"
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
    assert data["variantCount"] == 4


def test_patch_data_null_deletes_key(client):
    """Sending `null` is the explicit "clear this field" sentinel —
    e.g. gen-done passes `{aiBrief: null}` to invalidate stale
    descriptions before vision re-runs. Any other field stays put."""
    n = _make_image_node(client)

    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"data": {"aiBrief": None}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "aiBrief" not in data
    # The clear didn't take down its neighbours.
    assert data["title"] == "Hero"
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
    assert data["mediaId"] == "abc"


def test_patch_data_overrides_existing_value(client):
    """A non-null value for an existing key replaces it — merge isn't
    "ignore conflicts", it's "shallow object spread"."""
    n = _make_image_node(client)

    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"data": {"prompt": "rewritten prompt", "variantCount": 1}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["prompt"] == "rewritten prompt"
    assert data["variantCount"] == 1
    # Unrelated keys preserved.
    assert data["title"] == "Hero"
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"


def test_patch_data_adds_new_key_without_touching_others(client):
    """Adding a new key (e.g. mediaIds after first gen) must not
    require listing every legacy key — that was the bug pattern."""
    n = _make_image_node(client)

    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"data": {"mediaIds": ["a", "b", "c"]}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mediaIds"] == ["a", "b", "c"]
    assert data["title"] == "Hero"
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
    assert data["aiBrief"] == "young woman in cream blouse"


def test_patch_data_chain_of_partial_updates_keeps_invariants(client):
    """Reproduce the actual sequence that lost aspectRatio in
    production: gen-done sets aspectRatio + clears aiBrief, then vision
    callback sets a fresh aiBrief moments later. After both, every
    field set by either step must still be present — neither call can
    erase the other's contribution."""
    n = _make_image_node(client)

    # Step 1 — gen-done: persist generation result, clear stale brief.
    client.patch(
        f"/api/nodes/{n['id']}",
        json={
            "data": {
                "mediaId": "new-media",
                "mediaIds": ["new-media", "v2"],
                "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                "variantCount": 2,
                "aiBrief": None,
            },
        },
    )

    # Step 2 — vision callback: sets fresh aiBrief, lists nothing else.
    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"data": {"aiBrief": "describes the new image"}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # Every field set by step 1 is still there — this was THE bug.
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_LANDSCAPE"
    assert data["mediaId"] == "new-media"
    assert data["mediaIds"] == ["new-media", "v2"]
    assert data["variantCount"] == 2
    # Step 2's value won.
    assert data["aiBrief"] == "describes the new image"
    # Pre-existing untouched fields still preserved.
    assert data["title"] == "Hero"
    assert data["prompt"] == "studio shot"


def test_patch_data_empty_dict_is_a_noop(client):
    """An empty `data: {}` patch must not erase the column — pydantic
    sees the key as set, but there's nothing to merge so the existing
    payload survives intact."""
    n = _make_image_node(client)

    r = client.patch(f"/api/nodes/{n['id']}", json={"data": {}})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["title"] == "Hero"
    assert data["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
    assert data["aiBrief"] == "young woman in cream blouse"
    assert data["mediaId"] == "abc"


def test_patch_non_data_fields_still_replace(client):
    """Merge semantic only applies to `data` — scalar columns like
    `x`, `status` keep the simple setattr semantic so e.g. moving a
    node doesn't accidentally try to merge coordinates."""
    n = _make_image_node(client)

    r = client.patch(
        f"/api/nodes/{n['id']}",
        json={"x": 999.0, "y": -123.0, "status": "running"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["x"] == 999.0
    assert out["y"] == -123.0
    assert out["status"] == "running"
    # Data column wasn't touched.
    assert out["data"]["title"] == "Hero"
    assert out["data"]["aspectRatio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"


def test_delete_node_cascades_edges(client):
    b = _make_board(client)
    a = client.post("/api/nodes", json={"board_id": b["id"], "type": "image"}).json()
    c = client.post("/api/nodes", json={"board_id": b["id"], "type": "image"}).json()
    e = client.post(
        "/api/edges",
        json={"board_id": b["id"], "source_id": a["id"], "target_id": c["id"]},
    ).json()

    r = client.delete(f"/api/nodes/{a['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert e["id"] in body["deleted_edges"]

    # edge is gone server-side
    detail = client.get(f"/api/boards/{b['id']}").json()
    assert detail["edges"] == []


def test_story_script_sample_video_creates_reference_asset(client, monkeypatch):
    """Sample-video frames must become real media refs, not just LLM context."""
    from flowboard.routes import nodes as nodes_route

    b = _make_board(client)
    story = client.post(
        "/api/nodes",
        json={
            "board_id": b["id"],
            "type": "story_script",
            "data": {"title": "Story", "prompt": "copy this dance"},
        },
    ).json()

    class _Provider:
        async def is_available(self):
            return True

        async def run(self, *_args, **_kwargs):
            return json.dumps(
                [
                    {
                        "title": "Cảnh 1",
                        "image_prompt": "same dancer in the room",
                        "video_prompt": "performing the same dance move",
                        "narration": "Nhảy theo mẫu.",
                    },
                    {
                        "title": "Cảnh 2",
                        "image_prompt": "same dancer continues",
                        "video_prompt": "continues the choreography",
                        "narration": "Tiếp tục động tác.",
                    },
                ]
            )

    monkeypatch.setattr(
        nodes_route.secrets,
        "read_active_providers",
        lambda: {"planner": "fake"},
    )
    monkeypatch.setattr(nodes_route.registry, "get_provider", lambda _name: _Provider())

    media_id = "11111111-1111-1111-1111-111111111111"

    class _Sdk:
        async def upload_image(self, **kwargs):
            assert kwargs["project_id"] == "abcd1234"
            assert kwargs["mime_type"] == "image/jpeg"
            return {"raw": {}, "media_id": media_id}

    monkeypatch.setattr(nodes_route, "get_flow_sdk", lambda: _Sdk())

    class _Frame:
        shape = (720, 1280, 3)

    class _Cap:
        def __init__(self, _path):
            self.idx = 0

        def get(self, prop):
            if prop == fake_cv2.CAP_PROP_FPS:
                return 8
            if prop == fake_cv2.CAP_PROP_FRAME_COUNT:
                return 8
            return 0

        def isOpened(self):
            return self.idx < 8

        def read(self):
            if self.idx >= 8:
                return False, None
            self.idx += 1
            return True, _Frame()

        def release(self):
            pass

    def _imwrite(path, _frame):
        Path(path).write_bytes(b"\xff\xd8\xff\xe0flowboard-test-frame")
        return True

    fake_cv2 = types.SimpleNamespace(
        CAP_PROP_FPS=1,
        CAP_PROP_FRAME_COUNT=2,
        COLOR_BGR2GRAY=3,
        VideoCapture=_Cap,
        imwrite=_imwrite,
    )

    class _YDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=True):
            return {"ext": "mp4"}

    fake_ytdlp = types.SimpleNamespace(YoutubeDL=_YDL)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_ytdlp)

    res = client.post(
        f"/api/nodes/story-script/{story['id']}/generate",
        json={
            "prompt": "copy this dance",
            "sampleVideoUrl": "https://example.com/dance.mp4",
            "projectId": "abcd1234",
        },
    )
    assert res.status_code == 200, res.text

    detail = client.get(f"/api/boards/{b['id']}").json()
    sample_refs = [
        n
        for n in detail["nodes"]
        if n["type"] == "visual_asset"
        and n["data"].get("sourceStoryScriptId") == story["id"]
    ]
    assert len(sample_refs) == 1
    sample_ref = sample_refs[0]
    assert sample_ref["data"]["mediaId"] == media_id
    assert sample_ref["data"]["aspectRatio"] == "IMAGE_ASPECT_RATIO_LANDSCAPE"

    # With a sample video, each scene now anchors to the real source frame at
    # that point in the timeline: one "done" image node per scene carrying the
    # uploaded frame, and that scene's clip starts from it (so the clip
    # reproduces the original's composition shot-by-shot). 2 scenes → 2 source
    # image nodes + 2 video nodes.
    image_nodes = sorted(
        [n for n in detail["nodes"] if n["type"] == "image"],
        key=lambda n: n["x"],
    )
    video_nodes = sorted(
        [n for n in detail["nodes"] if n["type"] == "video"],
        key=lambda n: n["x"],
    )
    assert len(image_nodes) == 2
    assert len(video_nodes) == 2

    # Source-frame image nodes are pre-rendered (status done) and carry the
    # uploaded source frame media id — no Imagen call is needed for them.
    for img in image_nodes:
        assert img["status"] == "done"
        assert img["data"]["mediaId"] == media_id
        assert img["data"].get("sourceVideoFrame") is True

    first_video_data = video_nodes[0]["data"]
    second_video_data = video_nodes[1]["data"]
    assert first_video_data["sequenceIndex"] == 0
    assert first_video_data["sequenceTotal"] == 2
    assert second_video_data["sequenceIndex"] == 1
    assert second_video_data["sequenceTotal"] == 2
    assert "Action beat for this clip: performing the same dance move" in first_video_data["prompt"]
    assert "continues the choreography" in second_video_data["prompt"]

    edges = detail["edges"]
    # Every clip keeps the sample-video identity reference attached.
    assert all(
        any(e["source_id"] == sample_ref["id"] and e["target_id"] == v["id"] for e in edges)
        for v in video_nodes
    )
    # Each clip's PRIMARY start frame is its own scene's source-frame image
    # node (a non-fallback edge), so it reproduces that shot.
    for img, vid in zip(image_nodes, video_nodes):
        assert any(
            e["source_id"] == img["id"]
            and e["target_id"] == vid["id"]
            and e.get("target_handle") != "fallback-start-image"
            for e in edges
        )


def test_story_script_character_reference_locks_identity_without_sample_video(client, monkeypatch):
    from flowboard.routes import nodes as nodes_route

    b = _make_board(client)
    character = client.post(
        "/api/nodes",
        json={
            "board_id": b["id"],
            "type": "character",
            "data": {
                "title": "Main character",
                "mediaId": "22222222-2222-2222-2222-222222222222",
                "mediaIds": ["22222222-2222-2222-2222-222222222222"],
            },
            "status": "done",
        },
    ).json()
    story = client.post(
        "/api/nodes",
        json={
            "board_id": b["id"],
            "type": "story_script",
            "data": {"title": "Story", "prompt": "make her dance"},
        },
    ).json()
    client.post(
        "/api/edges",
        json={
            "board_id": b["id"],
            "source_id": character["id"],
            "target_id": story["id"],
        },
    )

    class _Provider:
        async def is_available(self):
            return True

        async def run(self, *_args, **_kwargs):
            return json.dumps(
                [
                    {
                        "title": "Cảnh 1",
                        "image_prompt": "a neon stage dancer",
                        "video_prompt": "dancing with fast arm movement",
                        "narration": "Nhân vật bắt đầu nhảy.",
                    }
                ]
            )

    monkeypatch.setattr(
        nodes_route.secrets,
        "read_active_providers",
        lambda: {"planner": "fake"},
    )
    monkeypatch.setattr(nodes_route.registry, "get_provider", lambda _name: _Provider())

    res = client.post(
        f"/api/nodes/story-script/{story['id']}/generate",
        json={"prompt": "make her dance"},
    )
    assert res.status_code == 200, res.text

    detail = client.get(f"/api/boards/{b['id']}").json()
    image_node = next(n for n in detail["nodes"] if n["type"] == "image")
    video_node = next(n for n in detail["nodes"] if n["type"] == "video")

    assert "connected Character reference as the authoritative identity source" in image_node["data"]["prompt"]
    assert "not hidden by silhouette" in image_node["data"]["prompt"]
    assert "connected Character reference as the authoritative identity source" in video_node["data"]["prompt"]
    assert any(
        e["source_id"] == character["id"] and e["target_id"] == image_node["id"]
        for e in detail["edges"]
    )
    assert any(
        e["source_id"] == character["id"] and e["target_id"] == video_node["id"]
        for e in detail["edges"]
    )
