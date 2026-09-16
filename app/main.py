"""
TTB label verification prototype.

One process serves both the API and the page, so there is no separate
frontend build and no cross-origin configuration to get wrong.

Nothing is written to disk or to a database. Images live in memory for the
length of one request and are discarded.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .extract import extract
from .matching import verify

STATIC = Path(__file__).parent / "static"

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_BATCH = 300
CONCURRENCY = int(os.environ.get("BATCH_CONCURRENCY", "8"))

APPLICATION_FIELDS = [
    "brand_name", "class_type", "abv",
    "net_contents", "producer", "country_of_origin",
]

app = FastAPI(title="TTB label verification prototype", docs_url="/api/docs")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "key_configured": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def _read(upload: UploadFile, data: bytes) -> None:
    if not data:
        raise HTTPException(400, f"{upload.filename} is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"{upload.filename} is larger than 12 MB.")


async def _run_one(image: bytes, filename: str, content_type: str | None,
                   application: dict) -> dict:
    started = time.perf_counter()
    extracted = await extract(image, filename, content_type)
    result = verify(application, extracted)
    result["filename"] = filename
    result["legibility"] = extracted.get("legibility", "clear")
    result["legibility_note"] = extracted.get("legibility_note", "")
    result["seconds"] = round(time.perf_counter() - started, 2)
    return result


@app.post("/api/verify")
async def verify_one(
    label: UploadFile = File(...),
    brand_name: str = Form(""),
    class_type: str = Form(""),
    abv: str = Form(""),
    net_contents: str = Form(""),
    producer: str = Form(""),
    country_of_origin: str = Form(""),
):
    data = await label.read()
    _read(label, data)

    application = {
        "brand_name": brand_name, "class_type": class_type, "abv": abv,
        "net_contents": net_contents, "producer": producer,
        "country_of_origin": country_of_origin,
    }

    try:
        return await _run_one(data, label.filename or "label", label.content_type, application)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(502, str(e))


def _parse_csv(raw: bytes) -> dict[str, dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "filename" not in [
        (f or "").strip().lower() for f in reader.fieldnames
    ]:
        raise HTTPException(
            400, "The CSV needs a filename column naming the image for each row."
        )

    rows: dict[str, dict] = {}
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = clean.get("filename", "")
        if not name:
            continue
        rows[name.lower()] = {f: clean.get(f, "") for f in APPLICATION_FIELDS}
    if not rows:
        raise HTTPException(400, "No rows found in the CSV.")
    return rows


@app.post("/api/verify-batch")
async def verify_batch(
    applications: UploadFile = File(...),
    labels: list[UploadFile] = File(...),
):
    if len(labels) > MAX_BATCH:
        raise HTTPException(413, f"Batches are limited to {MAX_BATCH} labels.")

    rows = _parse_csv(await applications.read())

    images: list[tuple[str, bytes, str | None]] = []
    unmatched_images: list[str] = []
    for up in labels:
        data = await up.read()
        _read(up, data)
        name = up.filename or ""
        if name.lower() in rows:
            images.append((name, data, up.content_type))
        else:
            unmatched_images.append(name)

    matched = {n.lower() for n, _, _ in images}
    missing_images = [n for n in rows if n not in matched]

    if not images:
        raise HTTPException(
            400, "No uploaded image filenames matched the filename column in the CSV."
        )

    started = time.perf_counter()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def run(name: str, data: bytes, ctype: str | None) -> dict:
        async with gate:
            try:
                return await _run_one(data, name, ctype, rows[name.lower()])
            except Exception as e:  # one bad label must not sink the batch
                return {
                    "filename": name, "overall": "error",
                    "headline": "Could not be processed",
                    "error": str(e), "fields": [], "counts": {}, "seconds": 0,
                }

    results = await asyncio.gather(*(run(n, d, c) for n, d, c in images))
    elapsed = time.perf_counter() - started

    tally: dict[str, int] = {}
    for r in results:
        tally[r["overall"]] = tally.get(r["overall"], 0) + 1

    order = {"mismatch": 0, "error": 1, "review": 2, "match": 3}
    results = sorted(results, key=lambda r: (order.get(r["overall"], 9), r["filename"]))

    return JSONResponse({
        "results": results,
        "tally": tally,
        "processed": len(results),
        "seconds_total": round(elapsed, 2),
        "seconds_each": round(elapsed / max(len(results), 1), 2),
        "unmatched_images": unmatched_images,
        "missing_images": missing_images,
    })
