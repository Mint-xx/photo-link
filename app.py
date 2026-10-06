import os
import sys
import json
import html
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

if sys.stdout is None:
    sys.stdout = open(BASE_DIR / "stdout.log", "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(BASE_DIR / "stderr.log", "a", encoding="utf-8", buffering=1)

import hmac
import hashlib
import base64
import sqlite3
import secrets
import time
import datetime
import threading
from contextlib import contextmanager
from io import BytesIO

try:
    import qrcode
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

PHOTO_DIR = BASE_DIR / "uploads"
PHOTO_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "data.db"
LOG_PATH = BASE_DIR / "access.log"
STATS_PATH = BASE_DIR / "stats.json"

PUBLIC_BASE    = os.environ.get("PUBLIC_BASE",    "")   # 留空则使用请求 Host
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GUEST_PASSWORD = os.environ.get("GUEST_PASSWORD", "")
MAX_UPLOAD = 12 * 1024 * 1024
ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

PWD_LEN = 6
PWD_MAX = 10 ** PWD_LEN

VALID_QUALITY = ("tiny", "low", "standard", "high")
QUALITY_CN = {"tiny": "极省", "low": "省流", "standard": "标准", "high": "高清"}

RATE_CREATE_PER_HOUR = 30
RATE_LOOKUP_PER_MIN  = 10
RATE_VIEW_PER_MIN    = 30

ADMIN_PAGE_SIZE = 100

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # 开启 WAL 模式，提升并发读写性能
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token       TEXT PRIMARY KEY,
                password    TEXT,
                quality     TEXT,
                created_at  REAL NOT NULL,
                used_at     REAL,
                remote_ip   TEXT,
                user_agent  TEXT,
                size        INTEGER,
                visit_ip    TEXT,
                visit_at    REAL,
                upload_ms   INTEGER,
                speed_kbps  REAL,
                cam_ms      INTEGER,
                wait_ms     INTEGER,
                compress_ms INTEGER,
                total_ms    INTEGER
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()]
        if "visit_ip" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN visit_ip TEXT")
        if "visit_at" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN visit_at REAL")
        if "quality" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN quality TEXT")
        if "upload_ms" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN upload_ms INTEGER")
        if "speed_kbps" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN speed_kbps REAL")
        if "cam_ms" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN cam_ms INTEGER")
        if "wait_ms" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN wait_ms INTEGER")
        if "compress_ms" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN compress_ms INTEGER")
        if "total_ms" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN total_ms INTEGER")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL,
                token   TEXT,
                ip      TEXT,
                ua      TEXT,
                ts      REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_created ON tokens(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_password ON tokens(password)")
        # 新增：加速 /api/quality-stats 的查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_used_at ON tokens(used_at DESC)")


init_db()


CLEANUP_INTERVAL = 3600
UNUSED_TTL       = 24 * 3600


def cleanup_unused_once():
    cutoff = time.time() - UNUSED_TTL
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT token FROM tokens WHERE created_at < ? AND used_at IS NULL",
                (cutoff,)
            ).fetchall()

            deleted = 0
            for r in rows:
                token = r["token"]
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    f = PHOTO_DIR / f"{token}{ext}"
                    if f.exists():
                        f.unlink(missing_ok=True)
                conn.execute("DELETE FROM tokens WHERE token=?", (token,))
                deleted += 1

            if deleted:
                print(f"[清理] 删除 {deleted} 条 24 小时内未拍照的记录", flush=True)
    except Exception as e:
        print(f"[清理] 出错: {e}", flush=True)


def cleanup_loop():
    time.sleep(30)
    while True:
        cleanup_unused_once()
        time.sleep(CLEANUP_INTERVAL)


_bg = threading.Thread(target=cleanup_loop, daemon=True)
_bg.start()


RATE = {}
RATE_LOCK = threading.Lock()


def rate_limit(key: str, max_count: int, window_sec: int):
    now = time.time()
    with RATE_LOCK:
        rec = RATE.get(key)
        if not rec or now - rec[1] > window_sec:
            RATE[key] = [1, now]
            return
        if rec[0] >= max_count:
            wait = max(1, int(window_sec - (now - rec[1])))
            raise HTTPException(429, f"请求过于频繁，请 {wait} 秒后再试")
        rec[0] += 1


def new_id(n=8):
    with db() as conn:
        while True:
            t = "".join(secrets.choice(ALPHABET) for _ in range(n))
            hit = conn.execute("SELECT 1 FROM tokens WHERE token=?", (t,)).fetchone()
            if not hit:
                return t


def new_password():
    with db() as conn:
        for _ in range(100):
            pwd = f"{secrets.randbelow(PWD_MAX):0{PWD_LEN}d}"
            hit = conn.execute("SELECT 1 FROM tokens WHERE password=?", (pwd,)).fetchone()
            if not hit:
                return pwd
    raise HTTPException(503, "密码已满，无法生成新链接")


def base_url(request):
    if PUBLIC_BASE:
        return PUBLIC_BASE.rstrip("/")
    return str(request.base_url).rstrip("/")


def detect_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None, None


def find_photo_file(token):
    for ext, mt in ((".webp", "image/webp"),
                    (".jpg", "image/jpeg"),
                    (".png", "image/png")):
        p = PHOTO_DIR / f"{token}{ext}"
        if p.exists():
            return p, mt
    return None, None


ADMIN_COOKIE = "adm"
ADMIN_COOKIE_MAX_AGE = 30 * 86400
COOKIE_SALT = b"admin-cookie-salt-v2"   # 换盐值让旧 cookie 失效


def admin_cookie_value(role: str) -> str:
    """role = 'admin' 或 'guest'"""
    pwd = ADMIN_PASSWORD if role == "admin" else GUEST_PASSWORD
    msg = f"{role}:{pwd}".encode()
    return hmac.new(hashlib.sha256(COOKIE_SALT).digest(),
                    msg, hashlib.sha256).hexdigest()


def check_admin_cookie(request: Request):
    """
    返回 'admin' / 'guest' / None
    None 表示未登录或 cookie 无效
    """
    val = request.cookies.get(ADMIN_COOKIE, "")
    if not val or "." not in val:
        return None
    try:
        role, sig = val.split(".", 1)
    except Exception:
        return None
    if role not in ("admin", "guest"):
        return None
    if hmac.compare_digest(sig, admin_cookie_value(role)):
        return role
    return None


def client_ip(request):
    fwd = (request.headers.get("CF-Connecting-IP")
           or request.headers.get("X-Forwarded-For")
           or request.headers.get("X-Real-IP"))
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def fmt_time(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def log_access(ts, ip, token, filename, size):
    line = f"{ts} - IP: {ip} - ID: {token} - File: {filename} - {size} bytes\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def log_event(kind, token, ip, ua=""):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO events(kind, token, ip, ua, ts) VALUES(?,?,?,?,?)",
                (kind, token, ip, (ua or "")[:300], time.time())
            )
    except Exception:
        pass


def load_stats():
    if STATS_PATH.exists():
        try:
            return json.loads(STATS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"visits": 0, "created": 0}


def save_stats(s):
    try:
        STATS_PATH.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


FAILS = {}


def check_rate(token, ip):
    key = f"{token}:{ip}"
    now = time.time()
    rec = FAILS.get(key)
    if rec and rec[1] > now:
        wait = int(rec[1] - now)
        raise HTTPException(429, f"尝试次数过多，请 {wait} 秒后再试")


def record_fail(token, ip):
    key = f"{token}:{ip}"
    now = time.time()
    rec = FAILS.get(key, [0, 0])
    if rec[1] < now:
        rec = [0, 0]
    rec[0] += 1
    if rec[0] >= 5:
        rec[1] = now + 300
        rec[0] = 0
    FAILS[key] = rec


def clear_fail(token, ip):
    FAILS.pop(f"{token}:{ip}", None)


# ============================================================
# 主站 / 业务接口
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, background: BackgroundTasks):
    s = load_stats()
    s["visits"] = int(s.get("visits", 0)) + 1
    save_stats(s)

    background.add_task(
        log_event, "home", None, client_ip(request),
        request.headers.get("user-agent", "")
    )

    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/create")
def create_token(request: Request, background: BackgroundTasks, quality: str = "tiny"):
    ip = client_ip(request)
    rate_limit(f"create:{ip}", RATE_CREATE_PER_HOUR, 3600)

    if quality not in VALID_QUALITY:
        quality = "tiny"

    token = new_id()
    password = new_password()
    with db() as conn:
        conn.execute(
            "INSERT INTO tokens(token, password, quality, created_at) VALUES(?,?,?,?)",
            (token, password, quality, time.time()))
    s = load_stats()
    s["created"] = int(s.get("created", 0)) + 1
    save_stats(s)

    background.add_task(
        log_event, "create", token, ip,
        request.headers.get("user-agent", "")
    )

    return {"id": token, "password": password, "quality": quality}


@app.get("/api/stats")
def stats():
    s = load_stats()
    return {
        "visits": int(s.get("visits", 0)),
        "created": int(s.get("created", 0)),
    }


@app.get("/api/quality-stats")
def quality_stats():
    result = {}
    try:
        with db() as conn:
            rows = conn.execute("""
                SELECT quality, total_ms
                FROM tokens
                WHERE used_at IS NOT NULL
                  AND total_ms IS NOT NULL
                  AND total_ms > 0
                ORDER BY used_at DESC
                LIMIT 200
            """).fetchall()

        buckets = {}
        for r in rows:
            q = r["quality"] or "tiny"
            if q not in VALID_QUALITY:
                q = "tiny"
            buckets.setdefault(q, [])
            buckets[q].append(r["total_ms"])

        for q, arr in buckets.items():
            if not arr:
                continue
            result[q] = {
                "avg_ms": int(sum(arr) / len(arr)),
                "count": len(arr),
            }
    except Exception:
        pass

    return result


@app.get("/api/qrcode")
def get_qrcode(text: str = ""):
    if not _HAS_QRCODE:
        raise HTTPException(
            500,
            "服务器未安装 qrcode 库。请在命令行运行：pip install qrcode[pil]"
        )

    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "缺少 text 参数")
    if len(text) > 500:
        raise HTTPException(400, "text 过长（最多 500 字符）")

    try:
        img = qrcode.make(text)
        buf = BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
    except Exception as e:
        raise HTTPException(500, f"生成失败: {e}")

    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/lookup")
def lookup(request: Request, password: str = ""):
    ip = client_ip(request)
    rate_limit(f"lookup:{ip}", RATE_LOOKUP_PER_MIN, 60)

    pwd = password.strip()
    if len(pwd) != PWD_LEN or not pwd.isdigit():
        raise HTTPException(400, f"请输入 {PWD_LEN} 位数字")

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tokens WHERE password=? ORDER BY created_at DESC LIMIT 6",
            (pwd,)
        ).fetchall()

    items = []
    for r in rows:
        if r["used_at"]:
            status = "captured"
        elif r["visit_ip"]:
            status = "opened"
        else:
            status = "unopened"

        item = {
            "token": r["token"],
            "status": status,
            "quality": r["quality"] or "tiny",
            "visit_ip": r["visit_ip"] or "—",
            "visit_at": fmt_time(r["visit_at"]),
            "remote_ip": r["remote_ip"] or "—",
            "used_at": fmt_time(r["used_at"]),
            "created_at": fmt_time(r["created_at"]),
            "upload_ms": r["upload_ms"] or 0,
            "speed_kbps": round(r["speed_kbps"] or 0, 1),
            "cam_ms": r["cam_ms"] or 0,
            "wait_ms": r["wait_ms"] or 0,
            "compress_ms": r["compress_ms"] or 0,
            "total_ms": r["total_ms"] or 0,
        }

        if status == "captured":
            p, mt = find_photo_file(r["token"])
            if p:
                b64 = base64.b64encode(p.read_bytes()).decode()
                item["data_url"] = f"data:{mt};base64,{b64}"
            else:
                item["status"] = "opened"

        items.append(item)

    return {"items": items}


@app.get("/c/{token}", response_class=HTMLResponse)
async def capture_page(request: Request, token: str, background: BackgroundTasks):
    with db() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()

    if row is None:
        return HTMLResponse("<meta charset='utf-8'><h2>链接无效</h2>", status_code=404)
    if row["used_at"]:
        return HTMLResponse("<meta charset='utf-8'><h2>该链接已被使用</h2>", status_code=410)

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    background.add_task(log_event, "visit", token, ip, ua)

    if not row["visit_ip"]:
        with db() as conn:
            conn.execute(
                "UPDATE tokens SET visit_ip=?, visit_at=? WHERE token=? AND visit_ip IS NULL",
                (ip, time.time(), token)
            )

    quality = "tiny"
    try:
        quality = row["quality"] or "tiny"
    except Exception:
        pass
    if quality not in VALID_QUALITY:
        quality = "tiny"

    return templates.TemplateResponse("capture.html", {
        "request": request,
        "token": token,
        "quality": quality,
    })


@app.post("/upload/{token}")
async def upload(token: str, request: Request, background: BackgroundTasks, photo: UploadFile = File(...)):
    data = await photo.read()

    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "文件过大")

    ext, _mt = detect_image(data)
    if ext is None:
        raise HTTPException(400, "不是有效的图片")

    tmp = PHOTO_DIR / f".{token}.tmp"
    tmp.write_bytes(data)

    final = PHOTO_DIR / f"{token}{ext}"
    tmp.replace(final)

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    try:
        with db() as conn:
            cur = conn.execute(
                """UPDATE tokens
                   SET used_at=?, remote_ip=?, user_agent=?, size=?
                   WHERE token=? AND used_at IS NULL""",
                (time.time(), ip, ua[:300], len(data), token),
            )
            if cur.rowcount == 0:
                exists = conn.execute("SELECT 1 FROM tokens WHERE token=?", (token,)).fetchone()
                raise HTTPException(404 if exists is None else 409,
                                    "链接不存在" if exists is None else "链接已被使用")
    except HTTPException:
        final.unlink(missing_ok=True)
        raise
    except Exception:
        final.unlink(missing_ok=True)
        raise HTTPException(500, "服务器错误")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 日志和事件改为后台任务，不阻塞上传响应
    background.add_task(log_access, ts, ip, token, final.name, len(data))
    background.add_task(log_event, "capture", token, ip, ua)
    background.add_task(print, f"[✓] {ip} 拍照完成 ID={token} 文件={final.name} 大小={len(data)}B", flush=True)

    return {"ok": True, "id": token}


@app.get("/api/upload-stat/{token}")
def upload_stat(
    token: str,
    cam_ms: int = 0,
    wait_ms: int = 0,
    compress_ms: int = 0,
    upload_ms: int = 0,
    total_ms: int = 0,
    speed_kbps: float = 0,
):
    try:
        with db() as conn:
            conn.execute(
                """UPDATE tokens
                   SET cam_ms=?, wait_ms=?, compress_ms=?,
                       upload_ms=?, total_ms=?, speed_kbps=?
                   WHERE token=? AND used_at IS NOT NULL""",
                (cam_ms, wait_ms, compress_ms,
                 upload_ms, total_ms, speed_kbps, token)
            )
    except Exception:
        pass
    return {"ok": True}


@app.get("/v/{token}", response_class=HTMLResponse)
def view_page(request: Request, token: str):
    with db() as conn:
        row = conn.execute("SELECT password FROM tokens WHERE token=?", (token,)).fetchone()

    if row is None or not row["password"]:
        return HTMLResponse("<meta charset='utf-8'><h2>链接无效</h2>", status_code=404)

    p, _mt = find_photo_file(token)
    if not p:
        return HTMLResponse("<meta charset='utf-8'><h2>还没有收到照片</h2>", status_code=404)

    return templates.TemplateResponse("view_auth.html", {
        "request": request, "token": token,
    })


@app.get("/api/view/{token}")
def verify_view(token: str, request: Request, password: str = ""):
    ip = client_ip(request)
    rate_limit(f"view:{ip}", RATE_VIEW_PER_MIN, 60)
    check_rate(token, ip)

    with db() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()

    if row is None or not row["password"]:
        raise HTTPException(404, "链接无效")

    pwd = password.strip()
    if not hmac.compare_digest(pwd, row["password"]):
        record_fail(token, ip)
        raise HTTPException(403, "密码错误")

    clear_fail(token, ip)

    p, mt = find_photo_file(token)
    if p:
        b64 = base64.b64encode(p.read_bytes()).decode()
        return {
            "ok": True,
            "data_url": f"data:{mt};base64,{b64}",
            "token": row["token"],
            "quality": row["quality"] or "tiny",
            "visit_ip": row["visit_ip"] or "—",
            "visit_at": fmt_time(row["visit_at"]),
            "remote_ip": row["remote_ip"] or "—",
            "used_at": fmt_time(row["used_at"]),
            "size_kb": round((row["size"] or 0) / 1024, 1),
            "upload_ms": row["upload_ms"] or 0,
            "speed_kbps": round(row["speed_kbps"] or 0, 1),
            "cam_ms": row["cam_ms"] or 0,
            "wait_ms": row["wait_ms"] or 0,
            "compress_ms": row["compress_ms"] or 0,
            "total_ms": row["total_ms"] or 0,
        }

    raise HTTPException(404, "没有这张照片")


# ============================================================
# 管理后台（管理员 + 游客）
# ============================================================
@app.get("/admin", response_class=HTMLResponse)
def admin_login_page(
    request: Request,
    tab: str = "links",
    page: int = 1, q: str = "",
    vp: int = 1, vq: str = "",
    cp: int = 1, cq: str = "",
    hp: int = 1, hq: str = "",
):
    role = check_admin_cookie(request)
    if role:
        return _render_admin(
            request, role=role, tab=tab,
            page=page, q=q,
            vp=vp, vq=vq,
            cp=cp, cq=cq,
            hp=hp, hq=hq,
        )
    return templates.TemplateResponse("admin_login.html", {
        "request": request, "error": None,
    })


@app.post("/admin", response_class=HTMLResponse)
async def admin_login_submit(request: Request):
    form = await request.form()
    pwd = (form.get("password") or "").strip()

    role = None
    if hmac.compare_digest(pwd, ADMIN_PASSWORD):
        role = "admin"
    elif hmac.compare_digest(pwd, GUEST_PASSWORD):
        role = "guest"

    if role is None:
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "密码错误",
        })

    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE,
        f"{role}.{admin_cookie_value(role)}",
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


def _paginate(conn, base_sql, count_sql, args, page, size):
    try:
        page = int(page)
    except Exception:
        page = 1
    if page < 1:
        page = 1

    total = conn.execute(count_sql, args).fetchone()[0]
    total_pages = max(1, (total + size - 1) // size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * size

    rows = conn.execute(
        base_sql + " LIMIT ? OFFSET ?",
        args + (size, offset)
    ).fetchall()

    return rows, total, page, total_pages


def _render_admin(
    request: Request,
    role: str = "admin",
    tab: str = "links",
    page: int = 1, q: str = "",
    vp: int = 1, vq: str = "",
    cp: int = 1, cq: str = "",
    hp: int = 1, hq: str = "",
):
    q  = (q  or "").strip()[:50]
    vq = (vq or "").strip()[:50]
    cq = (cq or "").strip()[:50]
    hq = (hq or "").strip()[:50]

    with db() as conn:
        # 管理链接
        if q:
            where = "WHERE token LIKE ? OR password = ?"
            args = (f"%{q}%", q)
        else:
            where = ""
            args = ()

        token_rows, tok_total, page, tok_pages = _paginate(
            conn,
            f"SELECT * FROM tokens {where} ORDER BY created_at DESC",
            f"SELECT COUNT(*) FROM tokens {where}",
            args, page, ADMIN_PAGE_SIZE,
        )

        # 打开记录
        if vq:
            where_v = "WHERE kind='visit' AND (token LIKE ? OR ip LIKE ?)"
            args_v = (f"%{vq}%", f"%{vq}%")
        else:
            where_v = "WHERE kind='visit'"
            args_v = ()

        visit_rows, v_total, vp, v_pages = _paginate(
            conn,
            f"SELECT ts, ip, token FROM events {where_v} ORDER BY ts DESC",
            f"SELECT COUNT(*) FROM events {where_v}",
            args_v, vp, ADMIN_PAGE_SIZE,
        )

        # 创建记录
        if cq:
            where_c = "WHERE kind='create' AND (token LIKE ? OR ip LIKE ?)"
            args_c = (f"%{cq}%", f"%{cq}%")
        else:
            where_c = "WHERE kind='create'"
            args_c = ()

        create_rows, c_total, cp, c_pages = _paginate(
            conn,
            f"SELECT ts, ip, token FROM events {where_c} ORDER BY ts DESC",
            f"SELECT COUNT(*) FROM events {where_c}",
            args_c, cp, ADMIN_PAGE_SIZE,
        )

        # 首页访问
        if hq:
            where_h = "WHERE kind='home' AND ip LIKE ?"
            args_h = (f"%{hq}%",)
        else:
            where_h = "WHERE kind='home'"
            args_h = ()

        home_rows, h_total, hp, h_pages = _paginate(
            conn,
            f"SELECT ts, ip, ua FROM events {where_h} ORDER BY ts DESC",
            f"SELECT COUNT(*) FROM events {where_h}",
            args_h, hp, ADMIN_PAGE_SIZE,
        )

    items = [{
        "token": r["token"],
        "password": r["password"] or "——",
        "quality": QUALITY_CN.get(r["quality"] or "tiny", "极省"),
        "url": f"{base_url(request)}/c/{r['token']}",
        "view_url": f"{base_url(request)}/v/{r['token']}",
        "created_at": fmt_time(r["created_at"])[5:16],
        "used": bool(r["used_at"]),
        "size_kb": round((r["size"] or 0) / 1024),
        "visit_ip": r["visit_ip"] or "—",
        "visit_at": fmt_time(r["visit_at"])[5:16] if r["visit_at"] else "—",
        "remote_ip": r["remote_ip"] or "—",
        "used_at": fmt_time(r["used_at"])[5:16] if r["used_at"] else "—",
        "upload_ms": r["upload_ms"] or 0,
        "speed_kbps": round(r["speed_kbps"] or 0, 1),
        "cam_ms": r["cam_ms"] or 0,
        "wait_ms": r["wait_ms"] or 0,
        "compress_ms": r["compress_ms"] or 0,
        "total_ms": r["total_ms"] or 0,
    } for r in token_rows]

    visit_items = [{"ts": fmt_time(r["ts"]), "ip": r["ip"] or "—", "token": r["token"] or "—"} for r in visit_rows]
    create_items = [{"ts": fmt_time(r["ts"]), "ip": r["ip"] or "—", "token": r["token"] or "—"} for r in create_rows]
    home_items = [{"ts": fmt_time(r["ts"]), "ip": r["ip"] or "—", "ua": (r["ua"] or "")[:80]} for r in home_rows]

    p_tok = {"page": page, "total": tok_total, "total_pages": tok_pages, "q": q,
             "has_prev": page > 1, "has_next": page < tok_pages}
    p_vis = {"page": vp, "total": v_total, "total_pages": v_pages, "q": vq,
             "has_prev": vp > 1, "has_next": vp < v_pages}
    p_cre = {"page": cp, "total": c_total, "total_pages": c_pages, "q": cq,
             "has_prev": cp > 1, "has_next": cp < c_pages}
    p_hom = {"page": hp, "total": h_total, "total_pages": h_pages, "q": hq,
             "has_prev": hp > 1, "has_next": hp < h_pages}

    active_tab = tab if tab in ("links", "visits", "creates", "homes") else "links"

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "items": items,
        "visit_items": visit_items,
        "create_items": create_items,
        "home_items": home_items,
        "p_tok": p_tok,
        "p_vis": p_vis,
        "p_cre": p_cre,
        "p_hom": p_hom,
        "active_tab": active_tab,
        "page_size": ADMIN_PAGE_SIZE,
        "role": role,                    # ← 传给模板，用来判断是否显示删除按钮
    })


@app.get("/admin/photo/{token}", response_class=HTMLResponse)
def admin_photo(token: str, request: Request):
    if not check_admin_cookie(request):
        return HTMLResponse(
            "<meta charset='utf-8'><h2 style='font-family:sans-serif;padding:40px;'>无权限</h2>",
            status_code=403,
        )

    p, _mt = find_photo_file(token)
    target_ext = p.suffix if p else None

    if target_ext is None:
        return HTMLResponse(
            "<meta charset='utf-8'><h2 style='font-family:sans-serif;padding:40px;'>没有这张照片</h2>",
            status_code=404,
        )

    img_url = f"/admin/photo/file/{token}"
    download_name = f"{token}{target_ext}"

    token_e       = html.escape(token)
    img_url_e     = html.escape(img_url, quote=True)
    download_e    = html.escape(download_name, quote=True)

    return HTMLResponse(f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>照片 {token_e}</title>
<style>
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{
    margin: 0; background: #0b0b0c; color: #eaeaea;
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px;
  }}
  .card {{
    background: #16171a; border: 1px solid #26282d; border-radius: 16px;
    padding: 20px; width: 100%; max-width: 560px;
  }}
  .title {{ text-align: center; font-size: 14px; color: #8a8a8a; margin-bottom: 14px; font-family: ui-monospace, monospace; }}
  img {{
    width: 100%; max-height: 65vh; object-fit: contain; border-radius: 12px;
    box-shadow: 0 8px 40px #000a; display: block; margin: 0 auto; background: #000;
  }}
  .actions {{ margin-top: 16px; display: flex; gap: 10px; }}
  .actions a, .actions button {{
    flex: 1; text-align: center; text-decoration: none; padding: 12px;
    border-radius: 10px; font-size: 13px; font-weight: 600; border: 0;
    cursor: pointer; font-family: inherit; transition: all .2s ease;
  }}
  .actions a {{ background: #2563eb; color: #fff; }}
  .actions a:hover {{ background: #1d4ed8; }}
  .actions button {{ background: #26282d; color: #eaeaea; }}
  .actions button:hover {{ background: #33353b; }}
</style>
</head>
<body>
  <div class="card">
    <div class="title">{token_e}</div>
    <img src="{img_url_e}" alt="photo">
    <div class="actions">
      <a href="{img_url_e}" download="{download_e}">下载原图</a>
      <button onclick="history.length > 1 ? history.back() : location.href='/admin'">返回</button>
    </div>
  </div>
</body>
</html>""")


@app.get("/admin/photo/file/{token}")
def admin_photo_file(token: str, request: Request):
    if not check_admin_cookie(request):
        raise HTTPException(403, "无权限")

    p, mt = find_photo_file(token)
    if p:
        return FileResponse(p, media_type=mt)
    raise HTTPException(404, "没有这张照片")


@app.get("/api/delete/{token}")
def delete_token(token: str, request: Request):
    role = check_admin_cookie(request)
    if role != "admin":
        # 游客或未登录一律拒绝
        raise HTTPException(403, "无权限")

    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        (PHOTO_DIR / f"{token}{ext}").unlink(missing_ok=True)
    with db() as conn:
        conn.execute("DELETE FROM tokens WHERE token=?", (token,))
    return {"ok": True}


# ============================================================
# 批量删除（仅管理员）
# ============================================================
@app.post("/api/delete-batch")
async def delete_batch(request: Request):
    role = check_admin_cookie(request)
    if role != "admin":
        raise HTTPException(403, "无权限：仅管理员可批量删除")

    tokens = []
    try:
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                tokens = body.get("tokens", []) or []
        else:
            form = await request.form()
            tokens = form.getlist("tokens")
    except Exception:
        tokens = []

    # 清洗：去空白、去重、限制长度
    clean = []
    seen = set()
    for t in tokens:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or t in seen or len(t) > 32:
            continue
        seen.add(t)
        clean.append(t)

    if not clean:
        raise HTTPException(400, "没有选中任何记录")
    if len(clean) > 200:
        raise HTTPException(400, "一次最多删除 200 条")

    deleted = 0
    with db() as conn:
        for token in clean:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                (PHOTO_DIR / f"{token}{ext}").unlink(missing_ok=True)
            cur = conn.execute("DELETE FROM tokens WHERE token=?", (token,))
            if cur.rowcount > 0:
                deleted += 1

    return {"ok": True, "deleted": deleted}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5004)