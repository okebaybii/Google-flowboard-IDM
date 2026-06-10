import types

import pytest
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.routes import video_assembly as va


def _make_board(client, name="Assembly"):
    return client.post("/api/boards", json={"name": name}).json()


def _make_node(client, board_id, node_type, data=None, status="idle", x=0, y=0):
    return client.post(
        "/api/nodes",
        json={
            "board_id": board_id,
            "type": node_type,
            "data": data or {},
            "status": status,
            "x": x,
            "y": y,
        },
    ).json()


def _make_edge(client, board_id, source_id, target_id, **extra):
    payload = {"board_id": board_id, "source_id": source_id, "target_id": target_id}
    payload.update(extra)
    return client.post("/api/edges", json=payload).json()


class _NoopWorker:
    def enqueue(self, _request_id: int) -> None:
        pass


@pytest.mark.asyncio
async def test_batch_continues_chained_clip_from_fallback_when_previous_fails(
    client,
    monkeypatch,
):
    board = _make_board(client)
    assembly = _make_node(
        client,
        board["id"],
        "video_assembly",
        {"title": "Assembly"},
        x=1000,
    )
    base_img = _make_node(
        client,
        board["id"],
        "image",
        {
            "title": "Base",
            "prompt": "base frame",
            "mediaId": "img-base",
            "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        },
        status="done",
    )
    clip1 = _make_node(
        client,
        board["id"],
        "video",
        {
            "title": "Clip 1",
            "prompt": "first motion",
            "continuityMode": "chain",
            "sequenceIndex": 0,
            "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        },
        x=320,
    )
    clip2 = _make_node(
        client,
        board["id"],
        "video",
        {
            "title": "Clip 2",
            "prompt": "continue motion",
            "continuityMode": "chain",
            "sequenceIndex": 1,
            "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        },
        x=640,
    )
    _make_edge(client, board["id"], base_img["id"], clip1["id"])
    _make_edge(client, board["id"], clip1["id"], clip2["id"])
    _make_edge(
        client,
        board["id"],
        base_img["id"],
        clip2["id"],
        target_handle="fallback-start-image",
    )
    _make_edge(client, board["id"], clip1["id"], assembly["id"])
    _make_edge(client, board["id"], clip2["id"], assembly["id"])

    monkeypatch.setattr(va, "get_worker", lambda: _NoopWorker())

    request_params_by_node: dict[int, dict] = {}

    async def fake_await_request(request_id: int, timeout_s: float = 300.0, poll_s: float = 1.5):
        with get_session() as s:
            req = s.get(Request, request_id)
            assert req is not None
            request_params_by_node[req.node_id] = dict(req.params)
        if req.node_id == clip1["id"]:
            return types.SimpleNamespace(status="failed", error="CAPTCHA_FAILED", result={})
        return types.SimpleNamespace(
            status="done",
            error=None,
            result={"media_ids": ["vid-clip-2"]},
        )

    monkeypatch.setattr(va, "_await_request", fake_await_request)

    await va.run_batch_generation(
        assembly["id"],
        "abcd1234",
        "PAYGATE_TIER_ONE",
        batch_video_aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
    )

    assert request_params_by_node[clip1["id"]]["start_media_id"] == "img-base"
    assert request_params_by_node[clip2["id"]]["start_media_id"] == "img-base"

    detail = client.get(f"/api/boards/{board['id']}").json()
    nodes = {n["id"]: n for n in detail["nodes"]}
    assert nodes[clip1["id"]]["status"] == "error"
    assert nodes[clip1["id"]]["data"]["error"] == "CAPTCHA_FAILED"
    assert nodes[clip2["id"]]["status"] == "done"
    assert nodes[clip2["id"]]["data"]["mediaId"] == "vid-clip-2"
    assert nodes[clip2["id"]]["data"].get("error") != "upstream_failed"


@pytest.mark.asyncio
async def test_batch_reuses_errored_clip_that_already_has_media(client, monkeypatch):
    board = _make_board(client)
    assembly = _make_node(client, board["id"], "video_assembly", {"title": "Assembly"})
    clip = _make_node(
        client,
        board["id"],
        "video",
        {
            "title": "Rendered but stale error",
            "prompt": "keep rendered clip",
            "mediaId": "vid-existing",
            "mediaIds": ["vid-existing"],
            "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "error": "old transient error",
        },
        status="error",
    )
    _make_edge(client, board["id"], clip["id"], assembly["id"])

    monkeypatch.setattr(va, "get_worker", lambda: _NoopWorker())

    async def should_not_generate(*_args, **_kwargs):
        raise AssertionError("errored clip with media should be reused")

    monkeypatch.setattr(va, "_await_request", should_not_generate)

    await va.run_batch_generation(
        assembly["id"],
        "abcd1234",
        "PAYGATE_TIER_ONE",
        batch_video_aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
    )

    detail = client.get(f"/api/boards/{board['id']}").json()
    nodes = {n["id"]: n for n in detail["nodes"]}
    assert nodes[clip["id"]]["status"] == "done"
    assert nodes[clip["id"]]["data"]["mediaId"] == "vid-existing"
    assert "error" not in nodes[clip["id"]]["data"]
    assert nodes[assembly["id"]]["status"] == "done"

    with get_session() as s:
        assert s.exec(select(Request)).all() == []


@pytest.mark.asyncio
async def test_batch_retries_internal_video_error_with_stabilized_payload(
    client,
    monkeypatch,
):
    board = _make_board(client)
    assembly = _make_node(client, board["id"], "video_assembly", {"title": "Assembly"})
    base_img = _make_node(
        client,
        board["id"],
        "image",
        {
            "title": "Base",
            "prompt": "base frame",
            "mediaId": "img-base",
            "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        },
        status="done",
    )
    clip = _make_node(
        client,
        board["id"],
        "video",
        {
            "title": "Clip",
            "prompt": (
                "Continuous video sequence clip 1 of 1. Opening frame context: "
                "a long complex scene. Action beat for this clip: dancer spins "
                "with detailed hand gestures and fast camera motion."
            ),
            "sourceVideoPrompt": "dancer spins with detailed hand gestures",
            "continuityMode": "chain",
            "sequenceIndex": 0,
            "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        },
    )
    _make_edge(client, board["id"], base_img["id"], clip["id"])
    _make_edge(client, board["id"], clip["id"], assembly["id"])

    monkeypatch.setattr(va, "get_worker", lambda: _NoopWorker())

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(va.asyncio, "sleep", no_sleep)

    attempts: list[dict] = []

    async def fake_await_request(request_id: int, timeout_s: float = 300.0, poll_s: float = 1.5):
        with get_session() as s:
            req = s.get(Request, request_id)
            assert req is not None
            attempts.append(dict(req.params))
        if len(attempts) == 1:
            return types.SimpleNamespace(
                status="failed",
                error="Internal error encountered.",
                result={},
            )
        return types.SimpleNamespace(
            status="done",
            error=None,
            result={"media_ids": ["vid-retry-ok"]},
        )

    monkeypatch.setattr(va, "_await_request", fake_await_request)

    await va.run_batch_generation(
        assembly["id"],
        "abcd1234",
        "PAYGATE_TIER_ONE",
        video_quality="quality",
        batch_video_aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
    )

    assert len(attempts) == 2
    assert attempts[0]["video_quality"] == "quality"
    assert attempts[1]["video_quality"] == "fast"
    assert "Generate a stable short image-to-video clip" in attempts[1]["prompt"]
    assert "dancer spins with detailed hand gestures" in attempts[1]["prompt"]
    assert attempts[1]["start_media_id"] == "img-base"

    detail = client.get(f"/api/boards/{board['id']}").json()
    nodes = {n["id"]: n for n in detail["nodes"]}
    assert nodes[clip["id"]]["status"] == "done"
    assert nodes[clip["id"]]["data"]["mediaId"] == "vid-retry-ok"
    assert "error" not in nodes[clip["id"]]["data"]
    assert nodes[assembly["id"]]["status"] == "done"
