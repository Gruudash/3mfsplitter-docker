"""FastAPI application entry point."""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from .api.routes import router

app = FastAPI(
    title="3MF Splitter",
    description="Split .3mf files by color into individually printable parts",
    version="1.0.0",
)

_cors_origins_raw = os.environ.get("CORS_ALLOW_ORIGINS", "*")
_cors_origins = ["*"] if _cors_origins_raw.strip() == "*" \
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

# Serve the frontend SPA
_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_FRONTEND):
    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(os.path.join(_FRONTEND, "index.html"))
