from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models  # noqa: F401  registers tables on Base
from .db import Base, tagging_engine
from .routers import codebook, embeddings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=tagging_engine)
    yield


app = FastAPI(title="Tagging prototype", lifespan=lifespan)

# The frontend calls the backend directly (no Vite proxy), so it needs CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5176"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(codebook.router)
app.include_router(codebook.codes_router)
app.include_router(embeddings.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
