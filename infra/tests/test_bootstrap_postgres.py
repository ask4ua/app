"""Verify schema privileges against a disposable PostgreSQL instance (Entra tested in Azure)."""
import importlib.util
import os
from pathlib import Path
import unittest
import psycopg
from psycopg.conninfo import make_conninfo

spec = importlib.util.spec_from_file_location("bootstrap_live", Path(__file__).resolve().parents[1] / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
DSN = os.getenv("TEST_BOOTSTRAP_DATABASE_URL")


@unittest.skipUnless(DSN, "Set TEST_BOOTSTRAP_DATABASE_URL to a disposable PostgreSQL instance")
class PostgresBootstrapTests(unittest.TestCase):
    def test_app_schema_privileges(self):
        with psycopg.connect(DSN, autocommit=True) as root:
            root.execute("CREATE ROLE demoappadmin LOGIN CREATEDB CREATEROLE PASSWORD 'test-admin'")
            root.execute("CREATE ROLE demoapp LOGIN PASSWORD 'test-app'")
            root.execute("CREATE DATABASE demoapp OWNER demoappadmin")
        with psycopg.connect(make_conninfo(DSN, user="demoappadmin", password="test-admin", dbname="demoapp")) as admin:
            bootstrap.grant_access(admin)
        with psycopg.connect(make_conninfo(DSN, user="demoapp", password="test-app", dbname="demoapp")) as app:
            app.execute("CREATE TABLE smoke (value integer)")
            app.execute("INSERT INTO smoke VALUES (1)")
            self.assertEqual(app.execute("SELECT count(*) FROM smoke").fetchone()[0], 1)
            self.assertEqual(app.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                                         "WHERE rolname = current_user").fetchone(), (False, False, False))
