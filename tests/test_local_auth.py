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

    def login_with(self, client, remember):
        client.get("/login")
        with client.session_transaction() as stored_session:
            csrf = stored_session["csrf"]
        data = {"csrf": csrf, "username": "admin", "password": "taobaoadmin@1"}
        if remember:
            data["remember"] = "1"
        return client.post("/login", data=data)

    def test_remember_login_sets_persistent_session_cookie(self):
        client = app.test_client(authenticated=False)
        with patch.dict(os.environ, {
            "WORKBENCH_ADMIN_USERNAME": "admin",
            "WORKBENCH_ADMIN_PASSWORD": "taobaoadmin@1",
        }, clear=False):
            response = self.login_with(client, remember=True)
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as stored_session:
            self.assertTrue(stored_session.permanent)
        # 持久会话 cookie：带 Expires（30 天后过期）
        self.assertIn("Expires=", response.headers.get("Set-Cookie", ""))

    def test_plain_login_keeps_browser_session_cookie(self):
        client = app.test_client(authenticated=False)
        with patch.dict(os.environ, {
            "WORKBENCH_ADMIN_USERNAME": "admin",
            "WORKBENCH_ADMIN_PASSWORD": "taobaoadmin@1",
        }, clear=False):
            response = self.login_with(client, remember=False)
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as stored_session:
            self.assertFalse(stored_session.permanent)
        self.assertNotIn("Expires=", response.headers.get("Set-Cookie", ""))

    def test_login_page_never_cached(self):
        # 登录页必须 no-store：否则浏览器复用旧 HTML 里的 csrf token，会话 cookie
        # 失效后（关浏览器/过期）提交必现 Invalid form token 400。
        client = app.test_client(authenticated=False)
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        cache = response.headers.get("Cache-Control", "")
        self.assertIn("no-store", cache)

    def test_stale_csrf_redirects_back_with_hint_instead_of_bare_400(self):
        # 模拟"打开历史缓存登录页 + 会话 cookie 已失效"：提交旧 token 时，
        # 不应再裸 400（浏览器报 Bad Request 无从下手），而是带提示回到登录页。
        client = app.test_client(authenticated=False)
        response = client.post("/login", data={
            "csrf": "stale-token", "next": "/", "username": "admin", "password": "taobaoadmin@1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        # 提示出现在跳转后的登录页
        follow = client.get(response.headers["Location"])
        self.assertIn("页面会话已过期", follow.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
