"""Finano Site — standalone web service for finano.ai."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pyotp
from fastapi import FastAPI, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Config ──

class Settings(BaseSettings):
    bot_api_url: str = "http://host.docker.internal:8100"
    dashboard_password: str = ""
    dashboard_totp_secret: str = ""
    session_hours: int = 24
    db_path: str = "/data/site.db"
    host: str = "0.0.0.0"
    port: int = 8200
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Database ──

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_content (
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    page_id TEXT DEFAULT 'main',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (key, page_id)
);

CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    access_type TEXT DEFAULT 'public',
    access_password_hash TEXT,
    tags TEXT DEFAULT '[]',
    pinned INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dashboard_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_id TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT DEFAULT '',
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sc_page ON site_content(page_id);
CREATE INDEX IF NOT EXISTS idx_dh_dashboard ON dashboard_history(dashboard_id);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
"""

DEFAULTS = {
    "hero_title": "Twój głosowy asystent biznesowy",
    "hero_highlight": "Finano AI",
    "hero_description": "Rozmawiaj ze swoją firmą. Zarządzaj finansami, fakturami i CRM jednym głosem.",
    "hero_buttons": json.dumps([
        {"label": "Live Dashboard", "action": "dashboard", "style": "primary"},
        {"label": "Rocket.Chat", "url": "https://chat.finano.ai", "style": "secondary"},
    ], ensure_ascii=False),
    "widgets": json.dumps([
        {"type": "elevenlabs", "agent_id": "agent_4501kmbhfkraej7sapeywn03n3z9", "enabled": True},
    ], ensure_ascii=False),
    "theme_brand": "#6366f1",
    "theme_accent": "#22d3ee",
    "theme_dark": "#0f172a",
    "footer_text": "BIZNETO Sp. z o.o. — Rzeszów",
    "meta_description": "Finano AI — inteligentny asystent AI",
}

db_path = Path(settings.db_path)

async def db_init():
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            await db.execute("INSERT OR IGNORE INTO site_content (key, value, page_id) VALUES (?, ?, ?)", (k, v, "main"))
        await db.commit()

async def db_get_content(key: str | None = None, page_id: str = "main") -> dict[str, Any]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if key:
            cursor = await db.execute("SELECT key, value FROM site_content WHERE key = ? AND page_id = ?", (key, page_id))
            row = await cursor.fetchone()
            return dict(row) if row else {}
        cursor = await db.execute("SELECT key, value FROM site_content WHERE page_id = ? ORDER BY key", (page_id,))
        rows = await cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}

async def db_set_content(key: str, value: str, page_id: str = "main", changed_by: str = ""):
    async with aiosqlite.connect(db_path) as db:
        # Log history
        cursor = await db.execute("SELECT value FROM site_content WHERE key = ? AND page_id = ?", (key, page_id))
        old = await cursor.fetchone()
        if old:
            await db.execute("INSERT INTO dashboard_history (dashboard_id, key, old_value, new_value, changed_by) VALUES (?, ?, ?, ?, ?)",
                (page_id, key, old[0], value, changed_by))
        await db.execute(
            "INSERT INTO site_content (key, value, page_id) VALUES (?, ?, ?) ON CONFLICT(key, page_id) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, value, page_id))
        await db.commit()

async def db_delete_content(key: str, page_id: str = "main") -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM site_content WHERE key = ? AND page_id = ?", (key, page_id))
        await db.commit()
        return (cursor.rowcount or 0) > 0

async def db_list_content(page_id: str = "main") -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, value, page_id, updated_at FROM site_content WHERE page_id = ? ORDER BY key", (page_id,))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

# ── Sessions ──

_sessions: dict[str, float] = {}

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + settings.session_hours * 3600
    return token

def verify_session(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if not token or token not in _sessions or _sessions[token] < time.time():
        _sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ── WebSocket ──

_ws_clients: set[WebSocket] = set()
_ws_public: set[WebSocket] = set()

async def broadcast(event_type: str, data: dict[str, Any]):
    global _ws_clients, _ws_public
    msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
    # Authenticated dashboard clients — full data
    if _ws_clients:
        dead: set[WebSocket] = set()
        for ws in _ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        _ws_clients -= dead
    # Public landing clients — only safe events (counters, cms)
    if _ws_public and event_type in ("message", "error", "cms_update"):
        safe = {"type": event_type, "ts": time.time()}
        if event_type == "message":
            safe["data"] = {"user": data.get("user", "?"), "tokens": data.get("tokens", 0)}
        elif event_type == "error":
            safe["data"] = {"count": 1}
        elif event_type == "cms_update":
            safe["data"] = data
        pub_msg = json.dumps(safe)
        dead_pub: set[WebSocket] = set()
        for ws in _ws_public:
            try:
                await ws.send_text(pub_msg)
            except Exception:
                dead_pub.add(ws)
        _ws_public -= dead_pub

# ── App ──

app = FastAPI(title="Finano Site")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    await db_init()
    # Migrate content from bot if our DB is fresh
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.bot_api_url}/api/dashboard/site-content")
            if r.status_code == 200:
                remote = r.json()
                if remote:
                    for k, v in remote.items():
                        await db_set_content(k, v)
                    logger.info("Migrated %d content keys from bot", len(remote))
    except Exception:
        pass

# ── Public API ──

@app.get("/api/site/content")
async def get_content(page_id: str = "main"):
    return await db_get_content(page_id=page_id)

@app.get("/api/site/public-stats")
async def public_stats():
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.bot_api_url}/api/dashboard/public-stats")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {"messages_today": 0, "tools_count": 0, "services_count": 0, "users_count": 0}

# ── Auth API ──

class LoginRequest(BaseModel):
    password: str
    totp_code: str

@app.post("/api/site/login")
async def login(req: LoginRequest):
    if not settings.dashboard_password:
        return {"error": "Auth not configured"}
    if req.password != settings.dashboard_password:
        return {"error": "Nieprawidłowe hasło"}
    if settings.dashboard_totp_secret:
        totp = pyotp.TOTP(settings.dashboard_totp_secret)
        if not totp.verify(req.totp_code, valid_window=1):
            return {"error": "Nieprawidłowy kod 2FA"}
    token = create_session()
    return {"token": token, "expires_in": settings.session_hours * 3600}

# ── Protected API ──

@app.get("/api/site/dashboard-stats")
async def dashboard_stats(_: bool = Depends(verify_session)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.bot_api_url}/api/dashboard/stats",
                           headers={"Authorization": "internal"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {"error": "Cannot reach bot API"}

@app.get("/api/site/dashboard-activity")
async def dashboard_activity(limit: int = 20, _: bool = Depends(verify_session)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.bot_api_url}/api/dashboard/activity",
                           params={"limit": limit},
                           headers={"Authorization": "internal"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {"activity": []}

@app.get("/api/site/dashboard-services")
async def dashboard_services(_: bool = Depends(verify_session)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.bot_api_url}/api/dashboard/services",
                           headers={"Authorization": "internal"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {"services": []}

# ── CMS Management API (for bot service registry) ──

@app.get("/api/site/cms/list")
async def cms_list():
    return {"items": await db_list_content(request.query_params.get("page_id", "main"))}

@app.post("/api/site/cms/set")
async def cms_set(request: Request):
    body = await request.json()
    key, value = body.get("key"), body.get("value")
    if not key or value is None:
        raise HTTPException(400, "key and value required")
    page_id = body.get("page_id", "main")
    await db_set_content(key, str(value), page_id=page_id)
    await broadcast("cms_update", {"key": key, "value": str(value), "page_id": page_id})
    return {"success": True, "key": key, "page_id": page_id}

@app.post("/api/site/cms/delete")
async def cms_delete(request: Request):
    body = await request.json()
    ok = await db_delete_content(body.get("key", ""), body.get("page_id", "main"))
    return {"success": ok}

# ── Dashboard CRUD ──

@app.post("/api/site/dashboards/create")
async def create_dashboard(request: Request):
    body = await request.json()
    dash_id = body.get("id") or secrets.token_urlsafe(8)
    title = body.get("title", "Untitled")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO dashboards (id, title, description, created_by, access_type, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (dash_id, title, body.get("description", ""), body.get("created_by", ""),
             body.get("access_type", "public"), json.dumps(body.get("tags", []))))
        await db.commit()
    # Set initial content
    for k, v in body.get("content", {}).items():
        await db_set_content(k, v if isinstance(v, str) else json.dumps(v), page_id=dash_id)
    return {"success": True, "id": dash_id, "url": f"https://finano.ai/d/{dash_id}"}

@app.get("/api/site/dashboards")
async def list_dashboards(archived: bool = False, limit: int = 20):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, description, access_type, tags, pinned, archived, created_at, updated_at FROM dashboards WHERE archived = ? ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (int(archived), limit))
        rows = await cursor.fetchall()
    return {"dashboards": [dict(r) for r in rows]}

@app.get("/api/site/dashboards/{dash_id}")
async def get_dashboard(dash_id: str):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM dashboards WHERE id = ?", (dash_id,))
        row = await cursor.fetchone()
    if not row:
        return {"error": "Dashboard not found"}
    content = await db_get_content(page_id=dash_id)
    d = dict(row)
    d.pop("access_password_hash", None)
    d["content"] = content
    return d

@app.post("/api/site/dashboards/{dash_id}/update")
async def update_dashboard(dash_id: str, request: Request):
    body = await request.json()
    sets, params = [], []
    for k in ("title", "description", "access_type", "pinned", "archived"):
        if k in body:
            sets.append(f"{k} = ?")
            params.append(body[k])
    if body.get("tags"):
        sets.append("tags = ?")
        params.append(json.dumps(body["tags"]))
    if not sets:
        return {"error": "Nothing to update"}
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(dash_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE dashboards SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()
    return {"success": True}

@app.delete("/api/site/dashboards/{dash_id}")
async def delete_dashboard(dash_id: str):
    if dash_id == "main":
        return {"error": "Cannot delete main page"}
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM dashboards WHERE id = ?", (dash_id,))
        await db.execute("DELETE FROM site_content WHERE page_id = ?", (dash_id,))
        await db.execute("DELETE FROM dashboard_history WHERE dashboard_id = ?", (dash_id,))
        await db.commit()
    return {"success": True}

@app.get("/api/site/dashboards/{dash_id}/history")
async def dashboard_history(dash_id: str, limit: int = 50):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT key, old_value, new_value, changed_by, changed_at FROM dashboard_history WHERE dashboard_id = ? ORDER BY changed_at DESC LIMIT ?",
            (dash_id, limit))
        rows = await cursor.fetchall()
    return {"history": [dict(r) for r in rows]}

# ── Event webhook (bot pushes events here) ──

@app.post("/api/site/event")
async def receive_event(request: Request):
    body = await request.json()
    event_type = body.get("type", "unknown")
    data = body.get("data", {})
    await broadcast(event_type, data)
    return {"ok": True}

# ── WebSocket ──

@app.websocket("/ws/public")
async def ws_public(websocket: WebSocket, page_id: str = "main"):
    await websocket.accept()
    websocket.state.page_id = page_id
    _ws_public.add(websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping", "ts": time.time()}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_public.discard(websocket)

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket, token: str = Query(default="")):
    session = _sessions.get(token)
    if not token or not session or session < time.time():
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping", "ts": time.time()}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(websocket)

# ── Static files (frontend) ──

@app.get("/manifest.json")
async def manifest():
    return FileResponse("/app/static/manifest.json", media_type="application/json")

@app.get("/sw.js")
async def sw():
    return FileResponse("/app/static/sw.js", media_type="application/javascript")

@app.get("/icon-192.png")
async def icon192():
    return FileResponse("/app/static/icon-192.png", media_type="image/png")

@app.get("/icon-512.png")
async def icon512():
    return FileResponse("/app/static/icon-512.png", media_type="image/png")

@app.get("/d/{dash_id}")
async def serve_dashboard(dash_id: str):
    return FileResponse("/app/static/index.html", media_type="text/html",
                       headers={"Cache-Control": "no-cache, no-store"})

@app.get("/{path:path}")
async def serve_frontend(path: str = ""):
    return FileResponse("/app/static/index.html", media_type="text/html",
                       headers={"Cache-Control": "no-cache, no-store"})
