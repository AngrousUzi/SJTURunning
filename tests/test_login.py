import unittest
from unittest.mock import Mock, patch

import requests

from src import login
from src.login import LoginError

PE_CALLBACK = "https://pe.sjtu.edu.cn/oauth2Login?code=x"


class Response:
    def __init__(self, url, body=None, status=200, text="", content=b"captcha"):
        self.url, self.body, self.status_code = url, body, status
        self.text, self.content = text, content
        self.headers = {"Content-Type": "image/jpeg"}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("private url must not escape")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class LoginSession(requests.Session):
    """Real parsing/submission code against a fake school."""

    def __init__(self, results=None, captcha=True, page_url=None):
        super().__init__()
        self.results = iter(results or [{"errno": 0, "url": PE_CALLBACK}])
        self.captcha = captcha
        self.page_url = page_url or "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=s"
        self.authenticated = False
        self.closed = False
        self.submissions = []
        self.pages = 0
        self.callback_final_url = "https://pe.sjtu.edu.cn/"

    def get(self, url, **kwargs):
        if url.startswith("https://pe.sjtu.edu.cn/oauth2Login"):
            self.authenticated = True
            self.cookies.set("JAAuthCookie", "v", domain="jaccount.sjtu.edu.cn")
            return Response(self.callback_final_url)
        if "/captcha" in url:
            return Response(url)
        self.pages += 1
        html = 'var ctx = {sid: "sid", uuid: "uuid-%d"};' % self.pages
        if self.captcha:
            html += '<img id="captcha-img">'
        return Response(self.page_url, text=html)

    def post(self, url, **kwargs):
        self.submissions.append(kwargs["data"])
        return Response(url, next(self.results))

    def close(self):
        self.closed = True
        super().close()


def run_login(session, solver=None, **kwargs):
    with patch("src.login.create_session", return_value=session):
        return login.login("user", "password", solver or Mock(return_value="abcd"), **kwargs)


class LoginTests(unittest.TestCase):
    def test_success_with_captcha(self):
        session = LoginSession()
        solver = Mock(return_value="abcde")
        self.assertIs(run_login(session, solver), session)
        self.assertTrue(session.authenticated)
        self.assertFalse(session.closed)
        self.assertEqual(session.submissions[0]["captcha"], "abcde")
        self.assertEqual(session.submissions[0]["pass"], "password")
        self.assertEqual(session.submissions[0]["lt"], "p")
        solver.assert_called_once_with(b"captcha")
        session.close()

    def test_success_without_captcha(self):
        session = LoginSession(captcha=False)
        solver = Mock(side_effect=AssertionError("must not run"))
        self.assertIs(run_login(session, solver), session)
        self.assertEqual(session.submissions[0]["captcha"], "")
        session.close()

    def test_create_session_never_retries_posts(self):
        session = login.create_session()
        self.assertEqual(session.adapters["https://"].max_retries.total, 0)
        self.assertIn("User-Agent", session.headers)
        session.close()

    def test_captcha_error_refreshes_form_then_succeeds(self):
        session = LoginSession([{"errno": 1, "error": "验证码错误"}, {"errno": 0, "url": PE_CALLBACK}])
        run_login(session, Mock(side_effect=["wrong", "right"]))
        self.assertEqual(len(session.submissions), 2)
        self.assertNotEqual(session.submissions[0]["uuid"], session.submissions[1]["uuid"])
        session.close()

    def test_credential_error_not_retried(self):
        session = LoginSession([{"errno": 1, "error": "用户名或密码错误"}])
        with self.assertRaises(LoginError) as caught:
            run_login(session)
        self.assertEqual(str(caught.exception), "用户名或密码错误")
        self.assertEqual(len(session.submissions), 1)
        self.assertTrue(session.closed)

    def test_unknown_error(self):
        session = LoginSession([{"errno": 2, "error": "需要短信验证"}])
        with self.assertRaises(LoginError) as caught:
            run_login(session)
        self.assertIn("交互式验证", str(caught.exception))
        self.assertEqual(len(session.submissions), 1)
        self.assertTrue(session.closed)

    def test_attempts_exhausted(self):
        session = LoginSession([{"errno": 1, "error": "captcha error"}] * 3)
        with self.assertRaises(LoginError) as caught:
            run_login(session)
        self.assertIn("验证码识别连续失败", str(caught.exception))
        self.assertEqual(len(session.submissions), 3)
        self.assertTrue(session.closed)

    def test_callback_host_rejected(self):
        session = LoginSession([{"errno": 0, "url": "https://foreign.example/callback"}])
        with self.assertRaises(LoginError):
            run_login(session)
        self.assertFalse(session.authenticated)
        self.assertTrue(session.closed)

    def test_final_host_not_pe_fails(self):
        session = LoginSession()
        session.callback_final_url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
        with self.assertRaises(LoginError):
            run_login(session)
        self.assertTrue(session.closed)

    def test_unexpected_login_page_host_or_missing_sid(self):
        session = LoginSession(page_url="https://example.com/x")
        with self.assertRaises(LoginError):
            run_login(session)
        self.assertTrue(session.closed)
        session = LoginSession(page_url="https://jaccount.sjtu.edu.cn/jaccount/jalogin")
        session.get = lambda url, **kw: Response(session.page_url, text="no params")
        with self.assertRaises(LoginError):
            run_login(session)
        self.assertEqual(session.submissions, [])

    def test_invalid_captcha_image_rejected(self):
        session = LoginSession()
        original = session.get

        def get(url, **kw):
            resp = original(url, **kw)
            if "/captcha" in url:
                resp.headers = {"Content-Type": "text/html"}
            return resp

        session.get = get
        with self.assertRaises(LoginError):
            run_login(session)
        self.assertEqual(session.submissions, [])


class SolverTests(unittest.TestCase):
    def _client(self, response):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        return client

    def test_validates_result_and_sends_only_image(self):
        for result, valid in [("abcde", True), ("abcd", True), ("<html>", False), (None, False), ("a" * 13, False)]:
            client = self._client(Response("solver", {"result": result}))
            with patch("src.login.requests.Session", return_value=client):
                if valid:
                    self.assertEqual(login.solve_captcha(b"image"), result)
                else:
                    with self.assertRaises(LoginError):
                        login.solve_captcha(b"image")
            kwargs = client.post.call_args.kwargs
            self.assertEqual(set(kwargs), {"files", "timeout", "allow_redirects"})
            self.assertEqual(kwargs["files"]["image"][1], b"image")
            self.assertFalse(kwargs["allow_redirects"])

    def test_non_200_rejected(self):
        client = self._client(Response("solver", {"result": "abcd"}, status=302))
        with patch("src.login.requests.Session", return_value=client):
            with self.assertRaises(LoginError):
                login.solve_captcha(b"image")

    def test_uses_separate_session_without_cookies(self):
        school = LoginSession()
        school.cookies.set("JAAuthCookie", "secret", domain="jaccount.sjtu.edu.cn")
        client = self._client(Response("solver", {"result": "abcd"}))
        with patch("src.login.requests.Session", return_value=client) as factory:
            login.solve_captcha(b"image")
        factory.assert_called_once_with()
        self.assertNotIn("headers", client.post.call_args.kwargs)
        self.assertNotIn("cookies", client.post.call_args.kwargs)
        school.close()


if __name__ == "__main__":
    unittest.main()
