def _auth_headers(client):
    res = client.post(
        "/api/auth/register-session",
        json={"id_token": "mock_social_block", "session_id": "social-session"},
    )
    assert res.status_code == 200
    return {
        "Authorization": "Bearer mock_social_block",
        "X-Session-ID": "social-session",
    }


def test_list_social_blocks_uses_real_db_session(client):
    headers = _auth_headers(client)
    res = client.get("/api/social-blocks", headers=headers)
    assert res.status_code == 200
    assert res.json() == []
