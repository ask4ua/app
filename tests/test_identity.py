"""Validate per-connection token refresh and preserve local Compose auth."""
from unittest.mock import MagicMock, patch
from api import main


def test_postgres_token_is_requested_for_every_connection(monkeypatch):
    monkeypatch.setenv('POSTGRES_AUTH', 'entra')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://demoapp@example/demoapp')
    credential = MagicMock()
    credential.get_token.side_effect = [MagicMock(token='first-token'), MagicMock(token='second-token')]
    with patch.object(main, 'azure_credential', return_value=credential), patch.object(main.psycopg, 'connect') as connect:
        main.db()
        main.db()
    assert [c.kwargs['password'] for c in connect.call_args_list] == ['first-token', 'second-token']
    assert connect.call_args.kwargs['sslmode'] == 'verify-full'


def test_local_database_uses_connection_string(monkeypatch):
    monkeypatch.delenv('POSTGRES_AUTH', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local:local@postgres/demoapp')
    with patch.object(main, 'azure_credential') as credential, patch.object(main.psycopg, 'connect') as connect:
        main.db()
    credential.assert_not_called()
    assert 'password' not in connect.call_args.kwargs


def test_blob_uses_identity_when_account_url_is_configured(monkeypatch):
    monkeypatch.setenv('AZURE_STORAGE_ACCOUNT_URL', 'https://example.blob.core.windows.net')
    monkeypatch.setenv('AZURE_STORAGE_CONTAINER', 'demoapp-staging')
    with patch.object(main, 'azure_credential', return_value='credential'), patch.object(main, 'BlobServiceClient') as client:
        main.storage()
    client.assert_called_once_with('https://example.blob.core.windows.net', credential='credential', api_version='2023-11-03')
    client.from_connection_string.assert_not_called()
