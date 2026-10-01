"""Run inside a workload-identity pod with the application's dependencies installed."""
import os
import certifi
import psycopg
from psycopg import sql
from uuid import uuid4

from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobServiceClient
from api.main import azure_credential, db, health, initialize, storage

initialize()
assert health() == {"status": "ok"}
with db() as conn:
    assert conn.execute("SELECT current_user").fetchone()["current_user"] == "demoapp"
    attributes = conn.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                              "WHERE rolname = current_user").fetchone()
    assert not any(attributes.values()), attributes
    table = sql.Identifier("identity_smoke_" + uuid4().hex)
    conn.execute(sql.SQL("CREATE TABLE {} (value text)").format(table))
    conn.execute(sql.SQL("INSERT INTO {} VALUES ('ok')").format(table))
    assert conn.execute(sql.SQL("SELECT value FROM {}").format(table)).fetchone()["value"] == "ok"
    conn.rollback()  # Exercise the app's schema privileges without leaving test tables.
key = f"smoke/{uuid4()}.txt"
with storage() as container:
    try:
        container.upload_blob(key, b"workload-identity-ok", overwrite=False)
        assert container.download_blob(key).readall() == b"workload-identity-ok"
    finally:
        container.delete_blob(key, delete_snapshots="include")
if os.getenv("PEER_STORAGE_ACCOUNT_URL"):
    with BlobServiceClient(os.environ["PEER_STORAGE_ACCOUNT_URL"], credential=azure_credential()) as peer:
        try:
            peer.get_container_client(os.environ["PEER_STORAGE_CONTAINER"]).get_container_properties()
        except HttpResponseError as error:
            assert error.status_code == 403, error.status_code
        else:
            raise AssertionError("Identity unexpectedly accessed the other environment")
if os.getenv("PEER_DATABASE_URL"):
    try:
        peer_db = psycopg.connect(os.environ["PEER_DATABASE_URL"],
            password=azure_credential().get_token('https://ossrdbms-aad.database.windows.net/.default').token,
            sslmode="verify-full", sslrootcert=certifi.where(), connect_timeout=15)
    except psycopg.OperationalError as error:
        # libpq connection errors may omit SQLSTATE; Azure emits this precise
        # identity-mismatch message. Do not mistake network/TLS errors for isolation.
        denied = error.sqlstate in {"28000", "28P01"} or (
            "FATAL:  Service principal oid mismatch for role" in str(error))
        assert denied, "Peer connection failed for a reason other than authentication"
    else:
        peer_db.close()
        raise AssertionError("Identity unexpectedly accessed the other database")
print("PASS: Entra DB login, non-admin SQL writes, Blob upload/download/delete, and environment isolation")
