"""Pytest configuration for the DCN backend.

Forces a fast, isolated SQLite database (instead of the MySQL configured in
.env) so the test suite runs without external services. These env overrides are
applied at conftest import time — before any test module imports the app — and
`dotenv.load_dotenv` does not overwrite existing env vars, so they win.

Tables are auto-created via DCN_AUTO_CREATE=true for the test session.
"""

import os
import tempfile

_db_path = os.path.join(tempfile.gettempdir(), "dcn_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("DCN_AUTO_CREATE", "true")
# Ensure required secrets exist even if .env is absent (CI).
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("CREDENTIAL_SECRET_KEY", "test-credential-secret-" + "y" * 32)
os.environ.setdefault("ADMIN_PASSWORD", "TestAdmin-123")
