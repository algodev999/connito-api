import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import cloudscraper
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("connito-api")

for _noisy in ("httpx", "httpcore", "hpack", "urllib3", "cloudscraper"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

# ── config ─────────────────────────────────────────────────────────────────────
CYCLE_API_URL = "https://cycle-api.connito.ai"
LB_API_URL    = "https://dashboard-api.connito.ai"
TIMEOUT       = 10.0
CACHE_TTL     = 2.0   # cycle endpoints — data changes every block
LB_CACHE_TTL  = 5.0   # leaderboard — heavier payload

CYCLE_ENDPOINTS = {
    "get_phase":               "/get_phase",
    "blocks_until_next_phase": "/blocks_until_next_phase",
}

# ── shared state ───────────────────────────────────────────────────────────────
_scraper:   cloudscraper.CloudScraper | None = None
_lb_client: httpx.AsyncClient | None         = None
_cache: dict[str, tuple[float, Any]]         = {}


@asynccontextmanager
async def lifespan(app):
    global _scraper, _lb_client

    _scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False}
    )
    _scraper.headers.update({"Accept": "application/json, */*"})

    _lb_client = httpx.AsyncClient(
        base_url=LB_API_URL,
        timeout=TIMEOUT,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        http2=True,
    )

    logger.info("Clients initialised (CloudScraper + httpx/HTTP2)")
    yield
    _scraper.close()
    await _lb_client.aclose()
    logger.info("Clients closed")


# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Connito Monitor", version="2.0.0", lifespan=lifespan)


# ── middleware ─────────────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    logger.debug("→ %s %s  client=%s", request.method, request.url.path,
                 request.client.host if request.client else "?")
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.DEBUG
    logger.log(level, "← %s %s  %d  %.1fms",
               request.method, request.url.path, response.status_code, elapsed_ms)
    return response


# ── cycle-api fetch (cloudscraper via thread pool) ─────────────────────────────
def _cycle_fetch_sync(path: str) -> Any | None:
    url = f"{CYCLE_API_URL}{path}"
    start = time.perf_counter()
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.debug("cycle OK  %s  %.1fms", path, (time.perf_counter() - start) * 1000)
        return data
    except Exception as exc:
        logger.warning("cycle ERR  %s  %s: %s", path, type(exc).__name__, exc)
        return None


async def _cycle_fetch(path: str) -> Any | None:
    now = time.monotonic()
    cached_at, cached_data = _cache.get(path, (0.0, None))
    if cached_data is not None and (now - cached_at) < CACHE_TTL:
        return cached_data
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _cycle_fetch_sync, path)
    if data is not None:
        _cache[path] = (time.monotonic(), data)
    return data


# ── leaderboard fetch (httpx async) ───────────────────────────────────────────
async def _lb_fetch() -> Any | None:
    now = time.monotonic()
    cached_at, cached_data = _cache.get("leaderboard", (0.0, None))
    if cached_data is not None and (now - cached_at) < LB_CACHE_TTL:
        return cached_data
    start = time.perf_counter()
    try:
        resp = await _lb_client.get("/api/v1/leaderboard")
        resp.raise_for_status()
        data = resp.json()
        logger.debug("leaderboard OK  %.1fms", (time.perf_counter() - start) * 1000)
        _cache["leaderboard"] = (time.monotonic(), data)
        return data
    except Exception as exc:
        logger.warning("leaderboard ERR  %s: %s", type(exc).__name__, exc)
        return None


# ── endpoints ──────────────────────────────────────────────────────────────────
@app.get("/api/get_phase")
async def get_phase():
    data = await _cycle_fetch("/get_phase")
    if data is None:
        raise HTTPException(502, "Upstream API unavailable")
    return data


@app.get("/api/blocks_until_next_phase")
async def blocks_until_next_phase():
    data = await _cycle_fetch("/blocks_until_next_phase")
    if data is None:
        raise HTTPException(502, "Upstream API unavailable")
    return data


@app.get("/api/leaderboard")
async def get_leaderboard():
    data = await _lb_fetch()
    if data is None:
        raise HTTPException(502, "Leaderboard API unavailable")
    return data


@app.get("/api/all")
async def get_all():
    results = await asyncio.gather(
        _cycle_fetch("/get_phase"),
        _cycle_fetch("/blocks_until_next_phase"),
    )
    return {
        "get_phase":               results[0],
        "blocks_until_next_phase": results[1],
    }


app.mount("/", StaticFiles(directory="static", html=True), name="static")
