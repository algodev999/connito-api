import asyncio
import logging
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx

# ── logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("connito-api")

# Quieten noisy third-party loggers but keep them at WARNING so problems still show
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Connito API Monitor", version="1.0.0")

BASE_URL = "https://cycle-api.connito.ai"
TIMEOUT = 10.0

ENDPOINTS = {
    "get_phase": "/get_phase",
    "blocks_until_next_phase": "/blocks_until_next_phase",
    "previous_phase_blocks": "/previous_phase_blocks",
    "get_validator_whitelist": "/get_validator_whitelist",
    "get_init_peer_id": "/get_init_peer_id",
}


# ── request middleware (logs every incoming HTTP hit) ──────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    logger.debug(
        "→ %s %s  client=%s",
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(
        level,
        "← %s %s  status=%d  %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ── upstream fetch helper ──────────────────────────────────────────────────────
async def _fetch(client: httpx.AsyncClient, path: str):
    url = f"{BASE_URL}{path}"
    logger.debug("upstream GET %s", url)
    start = time.perf_counter()
    try:
        resp = await client.get(url, timeout=TIMEOUT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        resp.raise_for_status()
        data = resp.json()
        logger.debug(
            "upstream OK  %s  status=%d  %.1fms  bytes=%d",
            path,
            resp.status_code,
            elapsed_ms,
            len(resp.content),
        )
        return data
    except httpx.HTTPStatusError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.warning(
            "upstream HTTP error  %s  status=%d  %.1fms",
            path,
            exc.response.status_code,
            elapsed_ms,
        )
        return None
    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.warning("upstream TIMEOUT  %s  %.1fms", path, elapsed_ms)
        return None
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error("upstream ERROR  %s  %.1fms  %s: %s", path, elapsed_ms, type(exc).__name__, exc)
        return None


# ── individual proxy endpoints ─────────────────────────────────────────────────
@app.get("/api/get_phase")
async def get_phase():
    logger.debug("handler: get_phase")
    async with httpx.AsyncClient() as client:
        data = await _fetch(client, "/get_phase")
    if data is None:
        logger.warning("get_phase: upstream unavailable — returning 502")
        raise HTTPException(502, "Upstream API unavailable")
    logger.info("get_phase: %s", data)
    return data


@app.get("/api/blocks_until_next_phase")
async def blocks_until_next_phase():
    logger.debug("handler: blocks_until_next_phase")
    async with httpx.AsyncClient() as client:
        data = await _fetch(client, "/blocks_until_next_phase")
    if data is None:
        logger.warning("blocks_until_next_phase: upstream unavailable — returning 502")
        raise HTTPException(502, "Upstream API unavailable")
    logger.info("blocks_until_next_phase: %s", data)
    return data


@app.get("/api/previous_phase_blocks")
async def previous_phase_blocks():
    logger.debug("handler: previous_phase_blocks")
    async with httpx.AsyncClient() as client:
        data = await _fetch(client, "/previous_phase_blocks")
    if data is None:
        logger.warning("previous_phase_blocks: upstream unavailable — returning 502")
        raise HTTPException(502, "Upstream API unavailable")
    logger.info("previous_phase_blocks: %s", data)
    return data


@app.get("/api/get_validator_whitelist")
async def get_validator_whitelist():
    logger.debug("handler: get_validator_whitelist")
    async with httpx.AsyncClient() as client:
        data = await _fetch(client, "/get_validator_whitelist")
    if data is None:
        logger.warning("get_validator_whitelist: upstream unavailable — returning 502")
        raise HTTPException(502, "Upstream API unavailable")
    logger.info("get_validator_whitelist: %d validators", len(data) if isinstance(data, list) else 1)
    return data


@app.get("/api/get_init_peer_id")
async def get_init_peer_id():
    logger.debug("handler: get_init_peer_id")
    async with httpx.AsyncClient() as client:
        data = await _fetch(client, "/get_init_peer_id")
    if data is None:
        logger.warning("get_init_peer_id: upstream unavailable — returning 502")
        raise HTTPException(502, "Upstream API unavailable")
    logger.info("get_init_peer_id: %s", data)
    return data


@app.get("/api/all")
async def get_all():
    """Fetch all five endpoints in parallel and return as a single payload."""
    logger.debug("handler: get_all — launching 5 parallel upstream fetches")
    start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _fetch(client, "/get_phase"),
            _fetch(client, "/blocks_until_next_phase"),
            _fetch(client, "/previous_phase_blocks"),
            _fetch(client, "/get_validator_whitelist"),
            _fetch(client, "/get_init_peer_id"),
        )
    elapsed_ms = (time.perf_counter() - start) * 1000
    failed = [k for k, v in zip(ENDPOINTS.keys(), results) if v is None]
    if failed:
        logger.warning("get_all: %d endpoint(s) failed: %s  total=%.1fms", len(failed), failed, elapsed_ms)
    else:
        logger.info("get_all: all 5 endpoints OK  total=%.1fms", elapsed_ms)

    return {
        "get_phase": results[0],
        "blocks_until_next_phase": results[1],
        "previous_phase_blocks": results[2],
        "get_validator_whitelist": results[3],
        "get_init_peer_id": results[4],
    }


app.mount("/", StaticFiles(directory="static", html=True), name="static")
