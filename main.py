import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Set

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
STORAGE_BUCKET = "captcha-images"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required environment variables"
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI(
    title="Argus Labeler API",
    description="API para revisão colaborativa de captchas de rotação.",
    version="2.0.0",
    docs_url="/swagger",
)

frontend_url = os.getenv("FRONTEND_URL", "*")
allowed_origins = [origin.strip() for origin in frontend_url.split(",") if origin.strip()]
if not allowed_origins:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

claims_memory: Dict[str, Dict[str, Any]] = {}
CLAIM_TTL_SECONDS = int(os.getenv("CLAIM_TTL_SECONDS", "300"))
METADATA_CACHE_TTL_SECONDS = int(os.getenv("METADATA_CACHE_TTL_SECONDS", "20"))
REVIEW_IDS_CACHE_TTL_SECONDS = int(os.getenv("REVIEW_IDS_CACHE_TTL_SECONDS", "5"))

_bucket_meta_cache: Dict[str, Any] = {
    "expires_at": 0.0,
    "available": [],
    "reviewable": [],
}
_reviewed_ids_cache: Dict[str, Any] = {
    "expires_at": 0.0,
    "ids": set(),
}
_angle_cache: Dict[str, float] = {}


class ReviewPayload(BaseModel):
    timestamp: str = Field(..., description="Sample timestamp identifier")
    angle: float = Field(..., description="Current angle selected by reviewer")
    angle_original: Optional[float] = Field(default=None, description="Original angle for optimization")
    action: Literal["confirmed", "adjusted", "skipped"]


class NextSampleResponse(BaseModel):
    timestamp: str
    angle_original: float
    reviewed: bool


def verify_key(key: str = Query(..., description="Access key")) -> str:
    expected = os.getenv("SECRET_KEY")
    if not expected or key != expected:
        raise HTTPException(status_code=401, detail="Invalid key")
    return key


def _extract_angle(data: Any) -> float:
    if isinstance(data, dict):
        for key in ["angle", "angle_original", "rotation", "value", "label"]:
            value = data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        for value in data.values():
            if isinstance(value, (int, float)):
                return float(value)
    if isinstance(data, (int, float)):
        return float(data)
    raise HTTPException(status_code=422, detail="Could not extract angle from label JSON")


def _list_bucket_files() -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    offset = 0
    limit = 100

    while True:
        batch = (
            supabase.storage.from_(STORAGE_BUCKET)
            .list(path="", options={"limit": limit, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
        )
        if not batch:
            break
        files.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return files


def _build_timestamps(files: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    inner: Set[str] = set()
    outer: Set[str] = set()
    labels: Set[str] = set()

    for item in files:
        name = item.get("name", "")
        if not isinstance(name, str):
            continue
        if name.endswith("_inner.jpg"):
            inner.add(name.removesuffix("_inner.jpg"))
        elif name.endswith("_outer.jpg"):
            outer.add(name.removesuffix("_outer.jpg"))
        elif name.endswith("_label.json"):
            labels.add(name.removesuffix("_label.json"))

    return {"inner": inner, "outer": outer, "labels": labels}


def _available_timestamps() -> List[str]:
    now = time.time()
    if now < float(_bucket_meta_cache["expires_at"]):
        return list(_bucket_meta_cache["available"])

    groups = _build_timestamps(_list_bucket_files())
    available = sorted(groups["inner"].intersection(groups["outer"]))
    reviewable = sorted(groups["inner"].intersection(groups["outer"]).intersection(groups["labels"]))

    _bucket_meta_cache["available"] = available
    _bucket_meta_cache["reviewable"] = reviewable
    _bucket_meta_cache["expires_at"] = now + METADATA_CACHE_TTL_SECONDS
    return list(available)


def _reviewable_timestamps() -> List[str]:
    now = time.time()
    if now < float(_bucket_meta_cache["expires_at"]):
        return list(_bucket_meta_cache["reviewable"])
    _available_timestamps()
    return list(_bucket_meta_cache["reviewable"])


def _get_reviewed_ids() -> Set[str]:
    now = time.time()
    if now < float(_reviewed_ids_cache["expires_at"]):
        return set(_reviewed_ids_cache["ids"])

    response = supabase.table("reviews").select("id").execute()
    data = response.data or []
    ids = {item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
    _reviewed_ids_cache["ids"] = ids
    _reviewed_ids_cache["expires_at"] = now + REVIEW_IDS_CACHE_TTL_SECONDS
    return set(ids)


def _invalidate_reviewed_ids_cache() -> None:
    _reviewed_ids_cache["expires_at"] = 0.0


def _public_image_url(filename: str) -> str:
    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(filename)
    if not public_url:
        raise HTTPException(status_code=404, detail="Image not found")
    return public_url


def _download_label_json(timestamp: str) -> Dict[str, Any]:
    try:
        binary_data = supabase.storage.from_(STORAGE_BUCKET).download(f"{timestamp}_label.json")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Label not found") from exc

    if isinstance(binary_data, bytes):
        raw_text = binary_data.decode("utf-8")
    else:
        raw_text = str(binary_data)

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Invalid label JSON") from exc

    if not isinstance(payload, dict):
        payload = {"value": payload}

    payload["angle_original"] = _extract_angle(payload)
    _angle_cache[timestamp] = float(payload["angle_original"])
    return payload


def _cleanup_claims(reviewed_ids: Set[str]) -> None:
    now_ts = datetime.now(timezone.utc).timestamp()
    expired: List[str] = []
    for timestamp, claim in claims_memory.items():
        claimed_at = float(claim.get("claimed_at", 0))
        if (now_ts - claimed_at) > CLAIM_TTL_SECONDS:
            expired.append(timestamp)
            continue
        if timestamp in reviewed_ids:
            expired.append(timestamp)
            continue
    for timestamp in expired:
        claims_memory.pop(timestamp, None)


def _is_claimed_by_other(timestamp: str, reviewer_id: str) -> bool:
    claim = claims_memory.get(timestamp)
    if not claim:
        return False
    return claim.get("reviewer_id") != reviewer_id


def _parse_exclude(exclude: Optional[str]) -> Set[str]:
    if not exclude:
        return set()
    return {part.strip() for part in exclude.split(",") if part.strip()}


def _claim_next_samples(
    reviewer_id: str,
    count: int,
    include_reviewed: bool = False,
    exclude: Optional[str] = None,
) -> List[Dict[str, Any]]:
    reviewed_ids = _get_reviewed_ids()
    _cleanup_claims(reviewed_ids)

    excluded = _parse_exclude(exclude)
    selected: List[Dict[str, Any]] = []
    selected_ids: Set[str] = set()

    def _attempt_select(allow_reviewed: bool) -> None:
        for timestamp in _reviewable_timestamps():
            if len(selected) >= count:
                break
            if (not allow_reviewed) and (timestamp in reviewed_ids):
                continue
            if timestamp in excluded or timestamp in selected_ids:
                continue
            if _is_claimed_by_other(timestamp, reviewer_id):
                continue

            angle_original = float(_download_label_json(timestamp)["angle_original"])
            claims_memory[timestamp] = {
                "reviewer_id": reviewer_id,
                "claimed_at": datetime.now(timezone.utc).timestamp(),
            }

            selected_ids.add(timestamp)
            selected.append(
                {
                    "timestamp": timestamp,
                    "angle_original": angle_original,
                    "reviewed": timestamp in reviewed_ids,
                    "inner_url": _public_image_url(f"{timestamp}_inner.jpg"),
                    "outer_url": _public_image_url(f"{timestamp}_outer.jpg"),
                }
            )

    _attempt_select(allow_reviewed=False)
    if include_reviewed and len(selected) < count:
        _attempt_select(allow_reviewed=True)

    return selected


@app.get("/samples")
def get_samples(_: str = Depends(verify_key)) -> List[Dict[str, Any]]:
    reviewed_ids = _get_reviewed_ids()
    _cleanup_claims(reviewed_ids)

    samples: List[Dict[str, Any]] = []
    for timestamp in _available_timestamps():
        angle_original: Optional[float]
        try:
            angle_original = float(_download_label_json(timestamp)["angle_original"])
        except HTTPException:
            angle_original = None

        samples.append(
            {
                "timestamp": timestamp,
                "angle_original": angle_original,
                "reviewed": timestamp in reviewed_ids,
            }
        )
    return samples


@app.get("/next-sample")
def get_next_sample(
    reviewer_id: str = Query(..., min_length=2, description="Reviewer identifier"),
    include_reviewed: bool = Query(default=False, description="Include already reviewed samples for reverification"),
    exclude: Optional[str] = Query(default=None, description="Comma-separated timestamps to skip"),
    _: str = Depends(verify_key),
) -> Dict[str, Any]:
    items = _claim_next_samples(
        reviewer_id=reviewer_id,
        count=1,
        include_reviewed=include_reviewed,
        exclude=exclude,
    )
    if items:
        return items[0]

    return {"sample": None}


@app.get("/next-samples")
def get_next_samples(
    reviewer_id: str = Query(..., min_length=2, description="Reviewer identifier"),
    count: int = Query(default=5, ge=1, le=20, description="Number of samples to claim"),
    include_reviewed: bool = Query(default=False, description="Include already reviewed samples for reverification"),
    exclude: Optional[str] = Query(default=None, description="Comma-separated timestamps to skip"),
    _: str = Depends(verify_key),
) -> List[Dict[str, Any]]:
    return _claim_next_samples(
        reviewer_id=reviewer_id,
        count=count,
        include_reviewed=include_reviewed,
        exclude=exclude,
    )
@app.get("/image/{filename}")
def get_image(filename: str, _: str = Depends(verify_key)) -> RedirectResponse:
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        public_url = _public_image_url(filename)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Image not found") from exc

    return RedirectResponse(url=public_url, status_code=302)


@app.get("/label/{timestamp}")
def get_label(timestamp: str, _: str = Depends(verify_key)) -> Dict[str, Any]:
    return _download_label_json(timestamp)


@app.post("/review")
def save_review(payload: ReviewPayload, _: str = Depends(verify_key)) -> Dict[str, Any]:
    angle_original = payload.angle_original
    if angle_original is None:
        cached_angle = _angle_cache.get(payload.timestamp)
        if cached_angle is not None:
            angle_original = float(cached_angle)
        else:
            angle_original = float(_download_label_json(payload.timestamp)["angle_original"])

    review_obj = {
        "id": payload.timestamp,
        "angle": float(payload.angle),
        "angle_original": float(angle_original),
        "action": payload.action,
        "raw_pixels": None,
        "slidebar_width": None,
        "icon_width": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "labeled_by": "human_web",
    }

    supabase.table("reviews").upsert(review_obj).execute()
    _invalidate_reviewed_ids_cache()
    claims_memory.pop(payload.timestamp, None)
    return {"ok": True, "review": review_obj}


@app.get("/export")
def export_reviews(_: str = Depends(verify_key)) -> JSONResponse:
    response = supabase.table("reviews").select("*").execute()
    reviews = response.data or []

    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(reviews),
        "reviews": reviews,
    }
    headers = {"Content-Disposition": "attachment; filename=reviews_export.json"}
    return JSONResponse(content=data, headers=headers)


@app.get("/progress")
def progress(_: str = Depends(verify_key)) -> Dict[str, int]:
    total = len(_available_timestamps())

    response = supabase.table("reviews").select("action").execute()
    rows = response.data or []

    confirmed = sum(1 for row in rows if row.get("action") == "confirmed")
    adjusted = sum(1 for row in rows if row.get("action") == "adjusted")
    skipped = sum(1 for row in rows if row.get("action") == "skipped")
    reviewed = confirmed + adjusted + skipped

    return {
        "total": total,
        "confirmed": confirmed,
        "adjusted": adjusted,
        "skipped": skipped,
        "remaining": max(total - reviewed, 0),
    }


@app.get("/")
def healthcheck() -> Dict[str, str]:
    return {"status": "ok", "service": "argus-labeler"}


@app.get("/healthz")
def render_healthcheck() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "argus-labeler",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/healthz/deep")
def render_healthcheck_deep() -> Dict[str, Any]:
    try:
        supabase.table("reviews").select("id").limit(1).execute()
        storage_ok = True
        db_ok = True
    except Exception:
        storage_ok = False
        db_ok = False

    status = "ok" if (storage_ok and db_ok) else "degraded"
    return {
        "status": status,
        "service": "argus-labeler",
        "supabase_db": db_ok,
        "supabase_storage": storage_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
