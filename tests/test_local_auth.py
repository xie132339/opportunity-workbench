"""Acceptance tests for the local login gate."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
import db


class LocalAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "auth.sqlite3")
        self.db_patch.start()
        db.initialize()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def login(self, client, *, next_path="/"):
        client.get(f"/login?next={next_path}")
        with client.session_transaction() as stored_session:
            csrf = stored_session["csrf"]
        return client.post("/login", data={
            "csrf": csrf,
            "next": next_path,
            "username": "admin",
            "password": "taobaoadmin@1",
        })

    def test_workbench_route_redirects_when_not_signed_in(self):
        client = app.test_client(authenticated=False)
        response = client.get("/sources?scope=enabled")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/sources?scope%3Denabled")

    def test_health_is_available_without_a_browser_session(self):
        response = app.test_client(authenticated=False).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_default_credentials_open_requested_internal_page(self):
        client = app.test_client(authenticated=False)
        with patch.dict(os.environ, {
            "WORKBENCH_ADMIN_USERNAME": "admin",
            "WORKBENCH_ADMIN_PASSWORD": "taobaoadmin@1",
        }, clear=False):
            response = self.login(client, next_path="/benefits")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/benefits")
        self.assertEqual(client.get("/benefits").status_code, 200)

    def test_wrong_password_does_not_open_workbench(self):
        client = app.test_client(authenticated=False)
        client.get("/login")
        with client.session_transaction() as stored_session:
            csrf = stored_session["csrf"]
        response = client.post("/login", data={
            "csrf": csrf, "username": "admin", "password": "wrong",
        })
        self.assertEqual(response.status_code, 401)
        self.assertIn("账号或密码不正确", response.get_data(as_text=True))
        self.assertEqual(client.get("/").status_code, 302)

    def test_login_rejects_external_return_target_and_logout_relocks(self):
        client = app.test_client(authenticated=False)
        with patch.dict(os.environ, {
            "WORKBENCH_ADMIN_USERNAME": "admin",
            "WORKBENCH_ADMIN_PASSWORD": "taobaoadmin@1",
        }, clear=False):
            response = self.login(client, next_path="//outside.example")
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(client.get("/").status_code, 200)
        with client.session_transaction() as stored_session:
            csrf = stored_session["csrf"]
        response = client.post("/logout", data={"csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(client.get("/").status_code, 302)


if __name__ == "__main__":
    unittest.main()
