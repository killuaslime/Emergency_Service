"""Закрытая карта отключений света.

Читает electricity.db только на чтение, бота не трогает.
Запуск: python map_panel/app.py  (слушает 127.0.0.1, наружу через nginx с HTTPS).
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "electricity.db"
USERS_FILE = BASE / "map_users.json"
SECRET = os.getenv("MAP_SECRET_KEY", "")
HOST = os.getenv("MAP_HOST", "127.0.0.1")
PORT = int(os.getenv("MAP_PORT", "8080"))
COOKIE_SECURE = os.getenv("MAP_COOKIE_SECURE", "1") == "1"  # 0 только для локальной проверки по http
COOKIE = "map_session"
SESSION_TTL = 12 * 3600
MSK = ZoneInfo("Europe/Moscow")

if len(SECRET) < 32:
    raise SystemExit("Задайте MAP_SECRET_KEY (32+ случайных символов) в .env")

_failures: dict[str, list[float]] = {}  # ip -> времена неудачных входов
MAX_FAILURES, BLOCK_SECONDS = 5, 600


# ---------- пароли и сессии ----------

def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    except Exception:
        return False


def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def make_token(username: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(
        {"u": username, "exp": int(time.time()) + SESSION_TTL}).encode())
    return f"{payload.decode()}.{_sign(payload)}"


def current_user(request: web.Request):
    token = request.cookies.get(COOKIE, "")
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode())):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data["exp"] < time.time():
            return None
        user = load_users().get(data["u"])  # права берём из файла: удалили пользователя, сессия умерла
        return {"name": data["u"], **user} if user else None
    except Exception:
        return None


def client_ip(request: web.Request) -> str:
    return request.headers.get("X-Real-IP") or request.remote or "?"


# ---------- страницы ----------

LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход</title>
<style>
:root{{color-scheme:light dark;--bg:#f3f5f7;--card:#fff;--ink:#1b2430;--mute:#66707d;--line:#d5dbe1;--acc:#c62f2b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14181d;--card:#1c222a;--ink:#e8ecf0;--mute:#98a2ae;--line:#323b46;--acc:#ef5a54}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.4 system-ui,"Segoe UI",Roboto,sans-serif}}
form{{width:min(320px,90vw);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:24px}}
h1{{font-size:20px;margin:0 0 16px}}label{{display:block;font-size:14px;color:var(--mute);margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:4px;background:transparent;color:inherit;font:inherit}}
input:focus-visible,button:focus-visible{{outline:2px solid var(--acc);outline-offset:1px}}
button{{margin-top:18px;width:100%;padding:10px;border:0;border-radius:4px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
.err{{color:var(--acc);font-size:14px;margin:12px 0 0}}
</style></head><body>
<form method="post" action="/login"><h1>Отключения света</h1>
<label for="u">Логин</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Пароль</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button>{error}</form></body></html>"""


def redirect(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


def login_page(error: str = "", status: int = 200) -> web.Response:
    html = LOGIN_HTML.format(error=f'<p class="err">{error}</p>' if error else "")
    return web.Response(text=html, content_type="text/html", status=status)


async def login_get(request):
    return login_page()


async def login_post(request):
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < BLOCK_SECONDS]
    _failures[ip] = recent
    if len(recent) >= MAX_FAILURES:
        return login_page("Слишком много попыток. Подождите 10 минут.", 429)

    form = await request.post()
    username, password = str(form.get("username", "")), str(form.get("password", ""))
    user = load_users().get(username)
    # проверяем хэш даже для несуществующего пользователя, чтобы не выдавать логины по времени ответа
    ok = verify_password(password, user["hash"] if user else "x$1$00$00")
    if not (user and ok):
        _failures[ip].append(now)
        return login_page("Неверный логин или пароль.", 401)

    _failures.pop(ip, None)
    resp = redirect("/")
    resp.set_cookie(COOKIE, make_token(username), max_age=SESSION_TTL,
                    httponly=True, secure=COOKIE_SECURE, samesite="Strict")
    return resp


async def logout(request):
    resp = redirect("/login")
    resp.del_cookie(COOKIE)
    return resp


async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")


# ---------- API ----------

SHOPS_SQL = """
SELECT s.code, s.address, s.lat, s.lon,
  (SELECT o.started_at FROM outages o
    WHERE o.shop_id = s.id AND o.ended_at IS NULL
    ORDER BY o.started_at DESC LIMIT 1) AS outage_since,
  EXISTS (SELECT 1 FROM outages o
    WHERE o.shop_id = s.id AND o.ended_at IS NOT NULL
      AND substr(o.ended_at, 1, 10) = :d) AS restored_today,
  dc.status AS morning
FROM shops s
LEFT JOIN daily_checks dc ON dc.shop_id = s.id AND dc.check_date = :d
WHERE (:sv IS NULL OR s.supervisor_id = :sv)
ORDER BY s.code
"""


def _minutes_since(iso: str, now: datetime) -> int:
    since = datetime.fromisoformat(iso)
    if since.tzinfo is None:
        since = since.replace(tzinfo=MSK)
    return max(0, int((now - since).total_seconds() // 60))


async def api_shops(request):
    user = request["user"]
    sv = user.get("supervisor_id") if user.get("role") == "supervisor" else None
    now = datetime.now(MSK)

    async with aiosqlite.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only = ON")
        rows = await (await db.execute(SHOPS_SQL, {"d": now.date().isoformat(), "sv": sv})).fetchall()

    shops, no_coords = [], []
    for r in rows:
        if r["lat"] is None or r["lon"] is None:
            no_coords.append(r["code"])
            continue
        if r["outage_since"]:
            status = "off"
        elif r["morning"] == "on" or r["restored_today"]:
            status = "on"
        else:
            status = "unknown"
        shops.append({
            "code": r["code"], "address": r["address"].strip(),
            "lat": r["lat"], "lon": r["lon"], "status": status,
            "since": r["outage_since"],
            "minutes": _minutes_since(r["outage_since"], now) if r["outage_since"] else None,
        })
    return web.json_response({"updated": now.isoformat(timespec="seconds"),
                              "shops": shops, "no_coords": no_coords})


# ---------- защита всех маршрутов ----------

PUBLIC = {"/login"}
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "img-src 'self' data: https://tile.openstreetmap.org https://cdnjs.cloudflare.com; "
       "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")


@web.middleware
async def guard(request, handler):
    if request.path not in PUBLIC:
        user = current_user(request)
        if not user:
            if request.path.startswith("/api/"):
                return web.json_response({"error": "unauthorized"}, status=401)
            return redirect("/login")
        request["user"] = user
    resp = await handler(request)
    resp.headers.update({
        "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    })
    return resp


def build_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index), web.get("/login", login_get), web.post("/login", login_post),
        web.post("/logout", logout), web.get("/api/shops", api_shops),
        web.static("/static", BASE / "static"),
    ])
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host=HOST, port=PORT)
