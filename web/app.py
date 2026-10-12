"""Web dashboard API and static PWA, served alongside the existing bot process."""
from __future__ import annotations
import os, secrets, logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from database import Database, repositories as repo
from .auth import verify_credentials, configured_admins

log = logging.getLogger("ffregistry.web")
ROOT = Path(__file__).resolve().parent
VALID_THEMES = {"esports", "modern", "pink", "aurora"}

class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

class ThemeBody(BaseModel):
    theme: str

class MatchBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: str = "solo"
    capacity: int = Field(ge=1, le=500)
    start_utc: str
    timezone: str = "Asia/Kolkata"
    group_id: int

class StatusBody(BaseModel):
    status: str


def create_web_app(db: Database) -> FastAPI:
    app = FastAPI(title="FFregistry Admin", docs_url=None, redoc_url=None)
    secret = (os.getenv("WEB_SESSION_SECRET") or "").strip()
    if len(secret) < 32:
        log.warning("WEB_SESSION_SECRET is missing/short; web sessions use an ephemeral secret and logins reset on restart")
        secret = secrets.token_urlsafe(48)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", https_only=os.getenv("WEB_COOKIE_SECURE", "true").lower() != "false", max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def current_user(request: Request) -> str:
        user = request.session.get("admin")
        if not isinstance(user, str) or not any(a["username"] == user for a in configured_admins()):
            raise HTTPException(status_code=401, detail="Please sign in again")
        return user

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/service-worker.js")
    async def service_worker():
        return FileResponse(ROOT / "static" / "service-worker.js", media_type="application/javascript", headers={"Cache-Control":"no-cache"})

    @app.get("/api/session")
    async def session(request: Request):
        user = request.session.get("admin")
        if not isinstance(user, str) or not any(a["username"] == user for a in configured_admins()):
            return {"authenticated": False, "adminsConfigured": len(configured_admins())}
        with db.read() as conn:
            row = conn.execute("SELECT theme FROM web_admin_preferences WHERE username=?", (user,)).fetchone()
        return {"authenticated": True, "username": user, "theme": row["theme"] if row else "esports"}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        if not configured_admins():
            raise HTTPException(503, "Set WEB_ADMIN1_USERNAME/PASSWORD and WEB_ADMIN2_USERNAME/PASSWORD in Railway variables first")
        if not verify_credentials(body.username, body.password):
            raise HTTPException(401, "Invalid username or password")
        request.session.clear()
        request.session["admin"] = body.username.strip()
        with db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO web_admin_preferences(username,theme,updated_at) VALUES(?,?,?)", (body.username.strip(), "esports", datetime.now(timezone.utc).isoformat()))
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, _: str = Depends(current_user)):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/overview")
    async def overview(_: str = Depends(current_user)):
        with db.read() as conn:
            matches = conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
            active = conn.execute("SELECT COUNT(*) n FROM matches WHERE status IN ('registration_open','registration_closed','in_progress')").fetchone()["n"]
            players = conn.execute("SELECT COUNT(DISTINCT telegram_id) n FROM registrations").fetchone()["n"]
            registrations = conn.execute("SELECT COUNT(*) n FROM registrations").fetchone()["n"]
            recent = conn.execute("SELECT m.*, (SELECT COUNT(*) FROM registrations r WHERE r.match_id=m.id) registered FROM matches m ORDER BY m.id DESC LIMIT 8").fetchall()
            groups = conn.execute("SELECT COUNT(*) n FROM groups WHERE authorized=1").fetchone()["n"]
        return {"matches": matches, "activeMatches": active, "players": players, "registrations": registrations, "groups": groups, "recent": [dict(r) for r in recent]}

    @app.get("/api/matches")
    async def matches(_: str = Depends(current_user)):
        with db.read() as conn:
            rows = conn.execute("SELECT m.*, g.title group_title, (SELECT COUNT(*) FROM registrations r WHERE r.match_id=m.id) registered FROM matches m LEFT JOIN groups g ON g.chat_id=m.group_id ORDER BY m.id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/matches/{match_id}/registrations")
    async def registrations(match_id: int, _: str = Depends(current_user)):
        with db.read() as conn:
            exists = conn.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
            if not exists: raise HTTPException(404, "Match not found")
            rows = conn.execute("SELECT id,match_id,telegram_id,username,ign,uid,slot,created_at FROM registrations WHERE match_id=? ORDER BY slot", (match_id,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/groups")
    async def groups(_: str = Depends(current_user)):
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM groups ORDER BY authorized DESC,title").fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/results")
    async def results(_: str = Depends(current_user)):
        with db.read() as conn:
            rows = conn.execute("SELECT res.*, m.name match_name, r.ign, t.name team_name FROM results res JOIN matches m ON m.id=res.match_id LEFT JOIN registrations r ON r.id=res.registration_id LEFT JOIN teams t ON t.id=res.team_id ORDER BY res.match_id DESC,res.placement ASC LIMIT 300").fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/theme")
    async def set_theme(body: ThemeBody, request: Request, user: str = Depends(current_user)):
        if body.theme not in VALID_THEMES: raise HTTPException(400, "Unknown theme")
        with db.transaction() as conn:
            conn.execute("INSERT INTO web_admin_preferences(username,theme,updated_at) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET theme=excluded.theme,updated_at=excluded.updated_at", (user, body.theme, datetime.now(timezone.utc).isoformat()))
        return {"ok": True, "theme": body.theme}

    @app.post("/api/matches")
    async def create_match(body: MatchBody, user: str = Depends(current_user)):
        if body.mode not in {"solo", "team", "custom"}: raise HTTPException(400, "Mode must be solo, team, or custom")
        try:
            datetime.fromisoformat(body.start_utc.replace("Z", "+00:00"))
        except ValueError: raise HTTPException(400, "start_utc must be ISO-8601 date/time")
        with db.transaction() as conn:
            group = conn.execute("SELECT chat_id FROM groups WHERE chat_id=? AND authorized=1", (body.group_id,)).fetchone()
            if not group: raise HTTPException(400, "Choose an authorized group")
            mid = repo.insert_match(conn, group_id=body.group_id, name=body.name.strip(), mode=body.mode, start_utc=body.start_utc, timezone=body.timezone, capacity=body.capacity, team_count=0, team_size=0, reg_deadline_utc=None, allow_cancel=1, allow_switch=1, created_by=0)
            conn.execute("INSERT INTO audit_logs(admin_id,match_id,action,target,details,created_at) VALUES(?,?,?,?,?,?)", (0,mid,"web_create_match",user,body.name.strip(),datetime.now(timezone.utc).isoformat()))
        return {"ok": True, "match_id": mid}

    @app.patch("/api/matches/{match_id}/status")
    async def set_status(match_id: int, body: StatusBody, user: str = Depends(current_user)):
        allowed = {"draft", "registration_open", "registration_closed", "in_progress", "completed", "cancelled"}
        if body.status not in allowed: raise HTTPException(400, "Invalid status")
        with db.transaction() as conn:
            row = conn.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
            if not row: raise HTTPException(404, "Match not found")
            conn.execute("UPDATE matches SET status=? WHERE id=?", (body.status, match_id))
            conn.execute("INSERT INTO audit_logs(admin_id,match_id,action,target,details,created_at) VALUES(?,?,?,?,?,?)", (0,match_id,"web_set_status",user,body.status,datetime.now(timezone.utc).isoformat()))
        return {"ok": True}

    return app
