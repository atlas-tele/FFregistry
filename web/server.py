"""Run the dashboard HTTP server in a daemon thread beside the polling bot."""
from __future__ import annotations
import logging, os, threading
import uvicorn
from database import Database
from .app import create_web_app

log = logging.getLogger("ffregistry.web")

def start_web_server(db: Database) -> threading.Thread:
    host = (os.getenv("WEB_HOST") or "0.0.0.0").strip()
    port = int(os.getenv("PORT") or os.getenv("WEB_PORT") or "8080")
    app = create_web_app(db)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False, proxy_headers=True, forwarded_allow_ips="*")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="ffregistry-web", daemon=True)
    thread.start()
    log.info("FFregistry dashboard server starting on %s:%s", host, port)
    return thread
