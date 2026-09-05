from catsyncd.auth import OAuthClient, load_oauth_client, _select_loopback_redirect


def test_uses_existing_localhost_redirect_from_client_json():
    client = OAuthClient(
        client_id="client-id",
        client_secret="secret",
        redirect_uris=("urn:ietf:wg:oauth:2.0:oob", "http://localhost"),
    )

    assert _select_loopback_redirect(client) == ("localhost", "localhost", "")


def test_falls_back_to_loopback_callback_when_no_usable_redirect_exists():
    client = OAuthClient(
        client_id="client-id",
        client_secret=None,
        redirect_uris=("urn:ietf:wg:oauth:2.0:oob",),
    )

    assert _select_loopback_redirect(client) == ("127.0.0.1", "127.0.0.1", "/oauth2callback")


def test_load_oauth_client_requires_user_file(tmp_path):
    from catsyncd.auth import AuthError
    import pytest

    with pytest.raises(AuthError):
        load_oauth_client(tmp_path / "missing.json")

    path = tmp_path / "client_secret.json"
    path.write_text('{"installed":{"client_id":"id","redirect_uris":["http://localhost"]}}')
    client = load_oauth_client(path)
    assert client.client_id == "id"
