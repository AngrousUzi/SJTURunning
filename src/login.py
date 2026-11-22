"""jAccount OAuth login for pe.sjtu.edu.cn with automatic captcha solving."""
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter

AUTHORIZE_URL = "https://jaccount.sjtu.edu.cn/oauth2/authorize"
CAPTCHA_URL = "https://jaccount.sjtu.edu.cn/jaccount/captcha"
LOGIN_POST_URL = "https://jaccount.sjtu.edu.cn/jaccount/ulogin"
SOLVER_URL = "https://geek.sjtu.edu.cn/captcha-solver/"
CLIENT_ID = "9mqzULSXYgUYj5fPOpyL"
REDIRECT_URI = "https://pe.sjtu.edu.cn/oauth2Login"
JACCOUNT_HOST = "jaccount.sjtu.edu.cn"
PE_HOST = "pe.sjtu.edu.cn"
MAX_ATTEMPTS = 3


class LoginError(Exception):
    """Login failure; the message is user-facing (shown by the UI)."""


def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    # A password submission must never be transparently retried by the transport.
    session.mount("https://", HTTPAdapter(max_retries=0))
    return session


def _get_login_page(session):
    """Enter via the pe.sjtu.edu.cn OAuth flow; return (login_url, html)."""
    resp = session.get(
        AUTHORIZE_URL,
        params={
            "response_type": "code",
            "scope": "profile",
            "client_id": CLIENT_ID,
            "state": "8",
            "redirect_uri": REDIRECT_URI,
        },
        allow_redirects=True,
        timeout=(10, 30),
    )
    resp.raise_for_status()
    return resp.url, resp.text


def _parse_login_form(html, url):
    """Return (params, uuid, has_captcha)."""
    js_params = {}
    for key in ("sid", "client", "returl", "se", "v", "uuid"):
        match = re.search(rf'{key}\s*:\s*"([^"]*)"', html)
        if match:
            js_params[key] = match.group(1)

    url_params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items() if v}
    params = {**url_params, **js_params}

    uuid = js_params.get("uuid") or url_params.get("uuid")
    if not uuid:
        match = re.search(r"captcha\?uuid=([0-9a-f-]+)", html)
        if match:
            uuid = match.group(1)

    has_captcha = bool(re.search(r"captcha", html, re.I))
    return params, uuid, has_captcha


def solve_captcha(image):
    """Solve a captcha image via the SJTU geek service (separate cookie-less session)."""
    try:
        with requests.Session() as client:
            response = client.post(
                SOLVER_URL,
                files={"image": ("captcha.jpg", image, "image/jpeg")},
                timeout=(10, 20),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise LoginError("验证码识别服务暂不可用，请稍后重试")
            try:
                text = response.json()["result"]
            except (ValueError, KeyError, TypeError):
                raise LoginError("验证码识别服务返回格式无效") from None
    except requests.RequestException:
        raise LoginError("无法连接验证码识别服务，请检查网络后重试") from None
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9]{1,12}", text.strip()):
        raise LoginError("验证码识别服务返回格式无效")
    return text.strip()


def _fetch_captcha(session, uuid, login_url):
    resp = session.get(
        CAPTCHA_URL,
        params={"uuid": uuid, "t": int(time.time() * 1000)},
        headers={"Referer": login_url},
        timeout=(10, 20),
    )
    resp.raise_for_status()
    if (not resp.headers.get("Content-Type", "").startswith("image/")
            or not 0 < len(resp.content) <= 1024 * 1024):
        raise LoginError("验证码图片格式无效")
    return resp.content


def _submit_login(session, params, uuid, username, password, captcha, login_url):
    resp = session.post(
        LOGIN_POST_URL,
        data={
            "sid": params.get("sid", ""),
            "client": params.get("client", ""),
            "returl": params.get("returl", ""),
            "se": params.get("se", ""),
            "v": params.get("v", ""),
            "uuid": uuid or "",
            "user": username,
            "pass": password,
            "captcha": captcha,
            "lt": "p",
        },
        headers={
            "Referer": login_url,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://jaccount.sjtu.edu.cn",
        },
        allow_redirects=False,
        timeout=(10, 30),
    )
    resp.raise_for_status()
    try:
        result = resp.json()
    except ValueError:
        raise LoginError("登录响应格式已变化，无法完成登录") from None
    if not isinstance(result, dict):
        raise LoginError("登录响应格式已变化，无法完成登录")
    return result


def _follow_callback(session, result):
    target = str(result.get("url") or "")
    if target.startswith("/"):
        target = "https://jaccount.sjtu.edu.cn" + target
    parts = urlsplit(target)
    if parts.scheme != "https" or parts.hostname not in (JACCOUNT_HOST, PE_HOST):
        raise LoginError("登录回调地址异常，已中止")
    resp = session.get(target, allow_redirects=True, timeout=(10, 30))
    final = urlsplit(resp.url)
    has_cookie = any(c.name == "JAAuthCookie" for c in session.cookies)
    if final.hostname != PE_HOST or not has_cookie:
        raise LoginError("登录未完成，请稍后重试")


def login(username, password, solver=None):
    """Log in to jAccount for pe.sjtu.edu.cn and return an authenticated Session."""
    solver = solver or solve_captcha
    session = create_session()
    try:
        for _ in range(MAX_ATTEMPTS):
            url, html = _get_login_page(session)
            if urlsplit(url).hostname != JACCOUNT_HOST:
                raise LoginError("登录页面结构已变化（意外跳转）")
            params, uuid, has_captcha = _parse_login_form(html, url)
            if not params.get("sid"):
                raise LoginError("登录页面结构已变化（缺少 sid）")
            captcha = ""
            if has_captcha:
                if not uuid:
                    raise LoginError("无法获取验证码")
                captcha = solver(_fetch_captcha(session, uuid, url))
            result = _submit_login(session, params, uuid, username, password, captcha, url)
            if result.get("errno") == 0:
                _follow_callback(session, result)
                return session
            error = str(result.get("error", "")).lower()
            if has_captcha and ("验证码" in error or "captcha" in error):
                continue
            if any(w in error for w in ("密码", "password", "用户名", "账号", "账户")):
                raise LoginError("用户名或密码错误")
            raise LoginError("登录失败，可能需要交互式验证（如短信/二次验证），请先在浏览器中登录 jAccount")
        raise LoginError("验证码识别连续失败，稍后重试")
    except requests.RequestException:
        session.close()
        raise LoginError("网络请求失败，请检查网络后重试") from None
    except BaseException:
        session.close()
        raise
