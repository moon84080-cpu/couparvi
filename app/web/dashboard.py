"""Phase 4 — 대시보드 (docs/04_ui_spec.md 기준).

FastAPI + Jinja2. 탭 4개(발굴/대본작성/미디어제작/게시검토)는 파이프라인 단계 1:1이며
소프트 게이팅(탭 이동 제약 없음, 큐가 비었을 때만 안내)이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    DEFAULT_FONT_KEY,
    DEFAULT_HOLD_MINUTES,
    FONT_LABELS,
    FONT_REGISTRY,
    NAVER_SEARCH_ENABLED,
    PARTNERS_DISCLOSURE,
)
from app.script.formats import active_tone_choices, get_format
from app.db import get_client
from app.discovery.education import detect_needs_education
from app.discovery.scoring import (
    MAX_PRICE_SCORE,
    MAX_REVIEW_COUNT_SCORE,
    MAX_STORY_SCORE,
    MAX_TOTAL_SCORE,
    ScoreInputs,
    calculate_score,
    score_review_count,
    score_story,
)
from app.discovery.story_heuristic import count_emotional_keywords
from app.media.images import ImageFetchError, download_image
from app.media.render import (
    CAPTION_FONTSIZE,
    CTA_CAPTION_FONTSIZE,
    HEIGHT,
    HOOK_FONTSIZE,
    STICKY_CTA_FONTSIZE,
    WIDTH,
    DURATION_MISMATCH_THRESHOLD_SEC,
    RenderError,
    predict_scene_duration_sec,
    recomposite_captions,
    resolve_font_path,
    resolve_scene_duration_sec,
    resolve_scene_text_elements,
)
from app.media.thumbnail import generate_thumbnail
from app.review.analyzer import ReviewAnalysisError, analyze_reviews
from app.review.ocr import ReviewOcrError, extract_review_text
from app.script.generator import ScriptGenerationError, generate_script
from app.script.image_matcher import ImageMatchError, select_scene_images
from app.script.validator import ScriptValidationError, validate_script
from app.upload.publisher import PublishError, publish_video
from app.web.auth import current_user, sign_in
from app.web.auth import AuthError

import csv
import io
import json
import os
import re

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="app/web/templates")
# styles.css를 브라우저가 캐싱해서, 배포/수정 후에도 강제 새로고침 없이는 옛 스타일이 계속
# 보이는 문제가 있었다 — 수정 시각을 쿼리스트링으로 붙여 파일이 바뀔 때마다 URL 자체가
# 바뀌게 해서 브라우저가 항상 새 버전을 받아오게 한다.
templates.env.globals["static_version"] = lambda: int(
    os.path.getmtime("app/web/static/styles.css")
)

DISCOVER_STATUSES = ["discovered", "scored", "reviews_collected"]
SCRIPT_STATUSES = ["analyzed", "script_generated"]
PROMPT_STATUSES = ["prompt_review"]
MEDIA_STATUSES = ["script_approved", "media_generated", "thumbnail_generated"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _policy_alert_count(client) -> int:
    result = client.table("policy_alerts").select("id").eq("reviewed", False).execute()
    return len(result.data)


def _base_ctx(request: Request, client) -> dict:
    return {
        "request": request,
        "user": current_user(request),
        "policy_alert_count": _policy_alert_count(client),
        "toast": request.query_params.get("toast"),
    }


def _counts(client) -> dict:
    def _count(statuses: list[str]) -> int:
        result = client.table("products").select("id").in_("status", statuses).execute()
        return len(result.data)

    return {
        "discover": _count(DISCOVER_STATUSES),
        "script": _count(SCRIPT_STATUSES),
        "prompts": _count(PROMPT_STATUSES),
        "media": _count(MEDIA_STATUSES),
        "publish": len(client.table("upload_queue").select("id").in_("status", ["pending_review", "ready_to_publish"]).execute().data),
    }


def _score_tier(score: int, max_score: int) -> str:
    """점수 grid 셀 배경색 구간 — 0점은 zero(중립), 나머지는 만점 대비 비율로 low/mid/high."""
    if not score:
        return "zero"
    ratio = score / max_score
    if ratio <= 0.33:
        return "low"
    if ratio <= 0.66:
        return "mid"
    return "high"


def _build_score_cells(product: dict) -> list[dict]:
    review_count = product.get("review_count") or 0
    review_count_score = score_review_count(review_count)
    fields = [
        ("리뷰수", f"{review_count}건", review_count_score, MAX_REVIEW_COUNT_SCORE),
        ("가격", str(product.get("price_score") or 0), product.get("price_score") or 0, MAX_PRICE_SCORE),
        ("스토리", str(product.get("story_score") or 0), product.get("story_score") or 0, MAX_STORY_SCORE),
    ]
    return [
        {"label": label, "display": display, "tier": _score_tier(score, max_score)}
        for label, display, score, max_score in fields
    ]


_STATUS_LABELS = {
    "discovered": "발굴됨",
    "scored": "점수산정",
    "reviews_collected": "리뷰수집",
    "analyzed": "분석완료",
    "script_generated": "대본생성",
    "prompt_review": "프롬프트확인중",
    "script_approved": "대본승인",
    "media_generated": "미디어제작",
    "thumbnail_generated": "썸네일완료",
    "queued_for_upload": "업로드대기",
    "uploaded": "게시완료",
}

# 파이프라인 진행 순서 — 뒤로 갈수록 뱃지 색이 진해지는 단계 판정에 쓴다 (1~5).
_STATUS_TIER = {
    "discovered": 1,
    "scored": 1,
    "reviews_collected": 1,
    "analyzed": 2,
    "script_generated": 2,
    "prompt_review": 2,
    "script_approved": 3,
    "media_generated": 3,
    "thumbnail_generated": 4,
    "queued_for_upload": 4,
    "uploaded": 5,
}


def _status_tab(status: str | None) -> str:
    if status in DISCOVER_STATUSES:
        return "discover"
    if status in SCRIPT_STATUSES:
        return "scripts"
    if status in PROMPT_STATUSES:
        return "prompts"
    if status in MEDIA_STATUSES:
        return "media"
    return "publish"


def _thumbnail_url_by_product_id(client, product_ids: list[str]) -> dict[str, str]:
    """상품별로 미디어제작에서 생성한 썸네일(있으면) URL을 매핑해서 돌려준다.

    products -> scripts -> render_jobs -> thumbnails 체인이라 한 번에 조인할 수 없어
    벌크 조회 두 번(scripts, render_jobs+thumbnails)으로 매핑을 만든다 — 상품마다
    개별 쿼리하면 최근 프로젝트 개수만큼 N배로 늘어난다.
    """
    if not product_ids:
        return {}
    scripts = client.table("scripts").select("id, product_id").in_("product_id", product_ids).execute().data
    script_id_to_product_id = {s["id"]: s["product_id"] for s in scripts}
    if not script_id_to_product_id:
        return {}

    render_jobs = (
        client.table("render_jobs")
        .select("id, script_id")
        .in_("script_id", list(script_id_to_product_id.keys()))
        .execute()
        .data
    )
    render_job_id_to_product_id = {
        rj["id"]: script_id_to_product_id[rj["script_id"]] for rj in render_jobs
    }
    if not render_job_id_to_product_id:
        return {}

    thumbnails = (
        client.table("thumbnails")
        .select("render_job_id, image_path, created_at")
        .in_("render_job_id", list(render_job_id_to_product_id.keys()))
        .eq("status", "done")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    url_by_product_id: dict[str, str] = {}
    for t in thumbnails:
        product_id = render_job_id_to_product_id.get(t["render_job_id"])
        # 정렬이 최신순이라 상품당 처음 만나는 썸네일이 가장 최근 것 — 그 이후는 무시한다.
        if product_id and product_id not in url_by_product_id and t.get("image_path"):
            url_by_product_id[product_id] = "/" + t["image_path"].replace("\\", "/")
    return url_by_product_id


def _recent_projects(client, limit: int = 6) -> list[dict]:
    """대시보드 하단 '최근 프로젝트' 리스트 — 상품을 최근 생성순으로 보여준다.

    미디어제작에서 썸네일을 생성한 상품은 원본 상품 사진 대신 그 썸네일을 보여준다
    (사용자 피드백) — 실제로 게시될 영상 표지와 대시보드 카드가 다르게 보이는 문제를
    없앤다.
    """
    rows = (
        client.table("products")
        .select("id, product_name, category, status, total_score, image_urls, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
    thumbnail_url_by_product_id = _thumbnail_url_by_product_id(client, [r["id"] for r in rows])
    projects = []
    for r in rows:
        status = r.get("status")
        image_urls = r.get("image_urls") or []
        thumbnail_url = thumbnail_url_by_product_id.get(r["id"])
        projects.append(
            {
                "id": r["id"],
                "name": r.get("product_name") or "(이름 미입력)",
                "category": r.get("category") or "카테고리 미입력",
                "status_label": _STATUS_LABELS.get(status, status or "-"),
                "status_tier": _STATUS_TIER.get(status, 1),
                "score": r.get("total_score") or 0,
                "image_url": thumbnail_url or (image_urls[0] if image_urls else None),
                "tab": _status_tab(status),
                "created_date": (r.get("created_at") or "")[:10],
            }
        )
    return projects


_DELETE_REDIRECT_TABS = {"discover", "scripts", "media"}


@router.post("/products/{product_id}/delete")
def delete_product(request: Request, product_id: str, redirect_to: str = Form("discover")):
    """발굴/대본작성/미디어제작 탭 공용 — 리스트 행의 삭제 아이콘에서 호출된다.

    products를 지우면 reviews/review_analysis/scripts/render_jobs 등은 FK cascade로 함께 삭제된다
    (migrations의 on delete cascade). 되돌릴 수 없어 프론트에서 confirm() 확인을 거친다.
    """
    client = get_client()
    client.table("products").delete().eq("id", product_id).execute()
    target = redirect_to if redirect_to in _DELETE_REDIRECT_TABS else "discover"
    return RedirectResponse(f"/{target}?toast=삭제했어요", status_code=302)


# --- 로그인 ---


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        user = sign_in(email, password)
    except AuthError as exc:
        return templates.TemplateResponse("login.html", {"request": request, "error": str(exc)}, status_code=401)
    request.session["user"] = user
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# --- 홈 ---


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    if current_user(request) is None:
        # "/"는 is_public_path()에서 예외적으로 공개 경로 취급된다 — 로그인 전에는
        # 마케팅 랜딩페이지를, 로그인 후에는 대시보드를 보여주도록 여기서 직접 가른다.
        return templates.TemplateResponse("landing.html", {"request": request})
    client = get_client()
    ctx = _base_ctx(request, client)
    counts = _counts(client)
    uploaded_today = len(
        client.table("products")
        .select("id")
        .eq("status", "uploaded")
        .gte("updated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00"))
        .execute()
        .data
    )
    now = datetime.now(timezone.utc)
    ctx.update(
        {
            "active": "home",
            "counts": counts,
            "daily_goal": 3,
            "uploaded_today": uploaded_today,
            "today_label": f"{now.year}년 {now.month}월 {now.day}일",
            # 대시보드는 처음엔 4개만 보여주고 "더보기"로 나머지를 펼친다 — 넉넉히 8개까지 미리 가져온다.
            "recent_projects": _recent_projects(client, limit=8),
        }
    )
    return templates.TemplateResponse("home.html", ctx)


# --- 발굴 탭 ---


@router.get("/discover", response_class=HTMLResponse)
def discover_tab(request: Request, selected: str | None = None, view: str = "list"):
    client = get_client()
    ctx = _base_ctx(request, client)
    products = (
        client.table("products")
        .select("*")
        .in_("status", DISCOVER_STATUSES)
        .order("total_score", desc=True)
        .execute()
        .data
    )
    selected_product = None
    latest_review_raw = ""
    if selected:
        found = [p for p in products if p["id"] == selected]
        if found:
            selected_product = found[0]
        else:
            # 상품선택 큐(발굴 단계)를 벗어난 상품이라도, 이전 단계로 돌아가 이미지 등
            # 누락 데이터를 채우러 온 경우일 수 있으니 상태와 무관하게 직접 조회한다.
            other = client.table("products").select("*").eq("id", selected).execute().data
            selected_product = other[0] if other else None
        if selected_product:
            latest_review = (
                client.table("reviews")
                .select("reviews_raw")
                .eq("product_id", selected_product["id"])
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
            )
            if latest_review:
                latest_review_raw = latest_review[0]["reviews_raw"] or ""
    ctx.update(
        {
            "active": "discover",
            "counts": _counts(client),
            "products": products,
            "selected": selected_product,
            "score_cells": _build_score_cells(selected_product) if selected_product else [],
            "latest_review_raw": latest_review_raw,
            "naver_search_enabled": NAVER_SEARCH_ENABLED,
            "view": view if selected_product else "list",
        }
    )
    return templates.TemplateResponse("tab_discover.html", ctx)


@router.post("/discover/new")
def discover_new(request: Request, mode: str = Form(...), value: str = Form(...)):
    client = get_client()
    if mode == "keyword":
        from app.coupang.partners import CoupangPartnersError, search_products

        try:
            results = search_products(value)
        except CoupangPartnersError:
            results = []
        if not results:
            return RedirectResponse("/discover?toast=" + "상품검색 실패 (쿠팡 API 확인 필요)", status_code=302)
        top = results[0]
        needs_education = detect_needs_education(top.category_name, top.product_name)
        breakdown = calculate_score(ScoreInputs(review_count=top.rating_count, price=top.product_price))
        row = {
            "coupang_url": top.product_url,
            "keyword": value,
            "product_name": top.product_name,
            "price": top.product_price,
            "image_urls": [top.product_image] if top.product_image else [],
            "deeplink": top.product_url,
            "category": top.category_name,
            "review_count": top.rating_count,
            "needs_education": needs_education,
            "status": "scored",
            **breakdown.as_dict(),
        }
    else:
        # mode == "manual": 쿠팡 API 없이 상품명만으로 최소 등록 (v2.2 임시 소싱)
        needs_education = detect_needs_education(None, value)
        breakdown = calculate_score(ScoreInputs())
        row = {
            "product_name": value,
            "needs_education": needs_education,
            "status": "scored",
            **breakdown.as_dict(),
        }
        # 네이버 쇼핑검색으로 대표 이미지를 자동 채움 시도 — 쿠팡과 무관, 실패해도 등록은 계속 진행.
        # 네이버 정책 변경으로 이 API가 막혀 NAVER_SEARCH_ENABLED=false인 동안은 아예 호출하지
        # 않는다(사용자 피드백, 2026-08-17).
        if NAVER_SEARCH_ENABLED:
            from app.discovery.naver_search import NaverSearchError, search_product_image

            try:
                image_result = search_product_image(value)
                if image_result and image_result.image:
                    row["image_urls"] = [image_result.image]
            except NaverSearchError:
                pass
    result = client.table("products").insert(row).execute()
    new_id = result.data[0]["id"]
    return RedirectResponse(f"/discover?selected={new_id}&view=detail&toast=상품을+등록했어요", status_code=302)


@router.post("/discover/extract-review-image")
async def discover_extract_review_image(file: UploadFile = File(...)):
    """리뷰 스크린샷 업로드 -> Claude vision으로 텍스트만 추출해 반환한다 (쿠팡 크롤링과 무관, JS fetch용)."""
    image_bytes = await file.read()
    media_type = file.content_type or "image/png"
    try:
        text = extract_review_text(image_bytes, media_type)
    except ReviewOcrError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"text": text})


def _image_content_hash(url: str) -> str | None:
    """이미지를 내려받아 MD5를 계산한다. 실패하면 None(그 이미지는 중복 판정에서 그냥 제외)."""
    import hashlib

    try:
        return hashlib.md5(download_image(url)).hexdigest()
    except ImageFetchError:
        return None


@router.get("/discover/{product_id}/image-candidates")
def discover_image_candidates(product_id: str, keyword: str):
    """네이버 쇼핑검색으로 후보 이미지 여러 개를 찾아 JS 피커에 보여준다 (JS fetch용).

    네이버는 같은 사진을 판매자마다 서로 다른 URL로 재호스팅하는 경우가 흔해서(실측 확인:
    URL은 다른데 바이트 단위로 완전히 동일한 이미지), URL만 비교해서는 중복을 못 잡는다.
    실제로 이미지를 내려받아 내용(MD5) 기준으로 중복을 걸러내고, 이미 상품에 추가해둔
    이미지와 내용이 같은 것도 함께 제외한다.
    """
    if not NAVER_SEARCH_ENABLED:
        # 네이버 정책 변경으로 쇼핑검색 API가 막혀 한시적으로 비활성화(사용자 피드백,
        # 2026-08-17) — 프론트도 버튼을 숨기지만, 직접 이 엔드포인트를 호출하는 경우까지 막는다.
        return JSONResponse({"error": "네이버 이미지 검색이 한시적으로 비활성화되어 있습니다."}, status_code=503)

    from app.discovery.naver_search import NaverSearchError, search_product_images

    client = get_client()
    product = client.table("products").select("image_urls").eq("id", product_id).execute().data[0]
    already_added = product.get("image_urls") or []

    try:
        results = search_product_images(keyword)
    except NaverSearchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    seen_hashes = {h for h in (_image_content_hash(u) for u in already_added) if h}
    seen_urls: set[str] = set()
    candidates = []
    for r in results:
        if not r.image or r.image in seen_urls:
            continue
        seen_urls.add(r.image)
        content_hash = _image_content_hash(r.image)
        if content_hash is None or content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        candidates.append({"image": r.image, "title": r.title})
    return JSONResponse({"candidates": candidates})


@router.post("/discover/{product_id}/images/add")
def discover_images_add(product_id: str, image_url: str = Form(...)):
    """네이버 후보 중 고른 이미지 URL 하나를 products.image_urls에 추가한다."""
    client = get_client()
    product = client.table("products").select("image_urls").eq("id", product_id).execute().data[0]
    image_urls = product.get("image_urls") or []
    if image_url not in image_urls:
        image_urls.append(image_url)
        client.table("products").update({"image_urls": image_urls}).eq("id", product_id).execute()
    return RedirectResponse(f"/discover?selected={product_id}&view=detail&toast=이미지를+추가했어요", status_code=302)


@router.post("/discover/{product_id}/images/upload")
async def discover_images_upload(product_id: str, file: UploadFile = File(...)):
    """상품 이미지를 로컬 파일로 직접 업로드해 products.image_urls에 추가한다."""
    client = get_client()
    product = client.table("products").select("image_urls").eq("id", product_id).execute().data[0]
    image_urls = product.get("image_urls") or []

    ext = os.path.splitext(file.filename or "")[1] or ".jpg"
    upload_dir = os.path.join("renders", "product_images", product_id)
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"{len(image_urls)}_{int(datetime.now(timezone.utc).timestamp())}{ext}"
    dest_path = os.path.join(upload_dir, filename)
    with open(dest_path, "wb") as f:
        f.write(await file.read())

    url = "/" + dest_path.replace(os.sep, "/")
    image_urls.append(url)
    client.table("products").update({"image_urls": image_urls}).eq("id", product_id).execute()
    return RedirectResponse(f"/discover?selected={product_id}&view=detail&toast=이미지를+업로드했어요", status_code=302)


@router.post("/discover/{product_id}/images/remove")
def discover_images_remove(product_id: str, image_url: str = Form(...)):
    """상품 이미지 하나를 products.image_urls에서 제거한다."""
    client = get_client()
    product = client.table("products").select("image_urls").eq("id", product_id).execute().data[0]
    image_urls = [u for u in (product.get("image_urls") or []) if u != image_url]
    client.table("products").update({"image_urls": image_urls}).eq("id", product_id).execute()
    return RedirectResponse(f"/discover?selected={product_id}&view=detail&toast=이미지를+삭제했어요", status_code=302)


@router.post("/discover/{product_id}/analyze")
def discover_analyze(
    request: Request,
    product_id: str,
    reviews_raw: str = Form(...),
    needs_education: str = Form(None),
    deeplink: str = Form(""),
    category: str = Form(""),
    next: str = Form("false"),
):
    client = get_client()
    product = client.table("products").select("*").eq("id", product_id).execute().data[0]

    client.table("reviews").insert({"product_id": product_id, "reviews_raw": reviews_raw}).execute()

    keyword_count = count_emotional_keywords(reviews_raw)
    new_story_score = score_story(keyword_count)
    old_story_score = product.get("story_score") or 0
    old_total_score = product.get("total_score") or 0
    new_total_score = min(old_total_score - old_story_score + new_story_score, MAX_TOTAL_SCORE)

    update_row = {
        "story_score": new_story_score,
        "total_score": new_total_score,
        "needs_education": needs_education == "on",
        "status": "reviews_collected",
    }
    if deeplink.strip():
        update_row["deeplink"] = deeplink.strip()
    if category.strip():
        update_row["category"] = category.strip()
    client.table("products").update(update_row).eq("id", product_id).execute()

    try:
        analysis_json = analyze_reviews(reviews_raw)
        review_row = client.table("reviews").select("*").eq("product_id", product_id).order("created_at", desc=True).limit(1).execute().data[0]
        client.table("review_analysis").insert({"review_id": review_row["id"], "analysis_json": analysis_json}).execute()
    except ReviewAnalysisError:
        return RedirectResponse(f"/discover?selected={product_id}&view=detail&toast=후기+분석에+실패했어요", status_code=302)

    # "분석 및 다음"(next=true)만 status를 analyzed로 올려 대본작성 탭으로 이동한다.
    # "분석하기"는 status를 reviews_collected로 유지해 상품선택 탭 큐에 그대로 남긴다
    # (DISCOVER_STATUSES에는 reviews_collected가, SCRIPT_STATUSES에는 analyzed만 포함되므로
    # status를 올려버리면 어느 탭에 머물든 상관없이 상품선택 큐에서 사라져버린다).
    if next == "true":
        client.table("products").update({"status": "analyzed"}).eq("id", product_id).execute()
        return RedirectResponse("/scripts?toast=분석+완료!+대본작성을+시작해보세요", status_code=302)
    return RedirectResponse(f"/discover?selected={product_id}&view=detail&toast=분석+완료!", status_code=302)


# --- 대본작성 탭 ---


@router.get("/scripts", response_class=HTMLResponse)
def scripts_tab(request: Request, selected: str | None = None, view: str = "list"):
    client = get_client()
    ctx = _base_ctx(request, client)
    products = (
        client.table("products")
        .select("*")
        .in_("status", SCRIPT_STATUSES)
        .order("total_score", desc=True)
        .execute()
        .data
    )
    selected_product = None
    latest_script = None
    tone_history = []
    if selected:
        found = [p for p in products if p["id"] == selected]
        if found:
            selected_product = found[0]
        else:
            # 대본작성 큐를 벗어난 상품(이전/다음 단계 이동)도 대본을 다시 확인·수정하러
            # 돌아올 수 있으니 상태와 무관하게 직접 조회한다.
            other = client.table("products").select("*").eq("id", selected).execute().data
            selected_product = other[0] if other else None
        if selected_product:
            scripts = (
                client.table("scripts")
                .select("*")
                .eq("product_id", selected)
                .order("created_at", desc=True)
                .execute()
                .data
            )
            latest_script = scripts[0] if scripts else None
            # 현재 표시 중인 최신 대본을 제외한 과거 생성분 — "생성한 톤" 기록을 목록으로
            # 보여주고 골라서 되돌리거나 개별 삭제할 수 있게 한다(사용자 피드백).
            tone_history = [
                {
                    "id": s["id"],
                    "tone": s["tone"],
                    "time": (s["created_at"] or "")[:16].replace("T", " "),
                }
                for s in scripts[1:]
            ]
    ctx.update(
        {
            "active": "script",
            "counts": _counts(client),
            "products": products,
            "selected": selected_product,
            "script": latest_script,
            "tones": active_tone_choices(),
            "tone_history": tone_history,
            "format": get_format(latest_script["tone"]) if latest_script else None,
            "view": view if selected_product else "list",
        }
    )
    return templates.TemplateResponse("tab_script.html", ctx)


@router.get("/scripts/check-product-name")
def scripts_check_product_name(product_name: str):
    """빠른 시작 폼에서 같은 이름의 상품이 이미 있는지 미리 물어보기 위한 조회 (JS fetch용).

    사용자 피드백(2026-08-17): 상품명을 착각해 중복 등록하는 걸 막고 싶다 — 등록 자체를
    막지는 않고, 프론트에서 confirm() 경고만 띄운다.
    """
    client = get_client()
    result = (
        client.table("products")
        .select("id")
        .eq("product_name", product_name)
        .limit(1)
        .execute()
    )
    return JSONResponse({"exists": bool(result.data)})


@router.post("/scripts/quick-start")
def scripts_quick_start(
    request: Request,
    product_name: str = Form(...),
    category: str = Form(""),
    needs_education: str = Form(None),
    tone: str = Form(...),
):
    """상품선택(발굴)의 리뷰입력/AI분석 단계를 건너뛰고, 상품명·카테고리·사실설명 여부만
    입력받아 빈 대본 틀을 만든 뒤 곧바로 대본 편집 화면으로 보낸다.

    AI 리뷰분석(analyze_reviews)도 AI 대본생성(generate_script)도 거치지 않는다 —
    나레이션/화면연출을 전부 사람이 직접 타이핑해서 채우고 싶다는 요청(사용자 피드백,
    2026-08-17). 편집 자체는 기존 "수정" 버튼(scripts_edit)이 그대로 처리하므로, 여기서는
    그 UI가 기대하는 모양(형식별 structure/scenes)만 빈 값으로 만들어둔다.
    """
    if tone not in active_tone_choices():
        raise HTTPException(status_code=400, detail=f"tone은 {active_tone_choices()} 중 하나여야 합니다.")

    client = get_client()

    detected_needs_education = detect_needs_education(category or None, product_name)
    final_needs_education = (
        (needs_education == "on") if needs_education is not None else detected_needs_education
    )
    breakdown = calculate_score(ScoreInputs())
    row = {
        "product_name": product_name,
        "needs_education": final_needs_education,
        "status": "scored",
        **breakdown.as_dict(),
    }
    if category.strip():
        row["category"] = category.strip()

    if NAVER_SEARCH_ENABLED:
        from app.discovery.naver_search import NaverSearchError, search_product_image

        try:
            image_result = search_product_image(product_name)
            if image_result and image_result.image:
                row["image_urls"] = [image_result.image]
        except NaverSearchError:
            pass

    product_id = client.table("products").insert(row).execute().data[0]["id"]

    fmt = get_format(tone)
    script_json = {
        "structure": {stage.key: "" for stage in fmt.stages},
        "educational_note": {"included": final_needs_education, "text": ""},
        "tone": tone,
        "scenes": [
            {
                "seq": i + 1,
                "stage": stage.key,
                "narration": "",
                "caption": "",
                "visual": "",
                "image_index": 0,
                "duration_sec": stage.seconds,
            }
            for i, stage in enumerate(fmt.stages)
        ],
        "disclosure": PARTNERS_DISCLOSURE,
        "estimated_duration_sec": sum(stage.seconds for stage in fmt.stages),
        "youtube": {"title": product_name, "description": "", "tags": []},
    }
    client.table("scripts").insert({"product_id": product_id, "tone": tone, "script_json": script_json}).execute()
    client.table("products").update({"status": "script_generated"}).eq("id", product_id).execute()

    return RedirectResponse(
        f"/scripts?selected={product_id}&view=detail&toast=등록+완료!+대본을+직접+작성해보세요", status_code=302
    )


@router.post("/scripts/{product_id}/generate")
def scripts_generate(request: Request, product_id: str, tone: str = Form(...)):
    client = get_client()
    product = client.table("products").select("*").eq("id", product_id).execute().data[0]
    review = client.table("reviews").select("*").eq("product_id", product_id).order("created_at", desc=True).limit(1).execute().data[0]
    analysis = client.table("review_analysis").select("*").eq("review_id", review["id"]).order("created_at", desc=True).limit(1).execute().data[0]

    needs_education = bool(product.get("needs_education", False))
    script_json = None
    last_error: Exception | None = None
    for attempt in range(2):  # 최초 시도 + 검증 실패 시 재시도 1회 (AGENTS.md 코딩 컨벤션)
        try:
            candidate = generate_script(
                analysis_json=analysis["analysis_json"], product=product, tone=tone, needs_education=needs_education
            )
            validate_script(candidate, review["reviews_raw"], needs_education)
            script_json = candidate
            break
        except (ScriptGenerationError, ScriptValidationError) as exc:
            last_error = exc
            continue

    if script_json is None:
        return RedirectResponse(f"/scripts?selected={product_id}&view=detail&toast=대본+생성+실패:+{str(last_error)[:60]}", status_code=302)

    # scenes[].image_index는 대본 생성 단계에서 LLM이 이미지를 실제로 보지 않고 순환 배정한
    # 임시값이다 — 여기서 visual(화면 연출) 문구와 후보 사진을 실제로 대조해 다시 고른다.
    # 실패해도 대본 생성 자체를 막을 정도는 아니라, 임시 배정값을 그대로 둔 채 진행한다.
    try:
        image_map = select_scene_images(script_json["scenes"], product.get("image_urls") or [])
        for scene in script_json["scenes"]:
            if scene["seq"] in image_map:
                scene["image_index"] = image_map[scene["seq"]]
    except ImageMatchError:
        pass

    client.table("scripts").insert(
        {
            "product_id": product_id,
            "analysis_id": analysis["id"],
            "tone": tone,
            "script_json": script_json,
        }
    ).execute()
    client.table("products").update({"status": "script_generated"}).eq("id", product_id).execute()
    return RedirectResponse(f"/scripts?selected={product_id}&view=detail&toast={tone}+톤으로+다시+생성했어요", status_code=302)


@router.post("/scripts/{product_id}/revert")
def scripts_revert(product_id: str, script_id: str = Form(...)):
    """과거에 생성했던 톤으로 되돌린다 — 그 대본 내용을 그대로 복사해 새 레코드로 다시
    insert해서 최신 대본으로 만든다(append-only 이력을 그대로 유지, 사용자 피드백)."""
    client = get_client()
    old = client.table("scripts").select("*").eq("id", script_id).eq("product_id", product_id).execute().data[0]
    client.table("scripts").insert(
        {
            "product_id": product_id,
            "analysis_id": old["analysis_id"],
            "tone": old["tone"],
            "script_json": old["script_json"],
        }
    ).execute()
    return RedirectResponse(f"/scripts?selected={product_id}&view=detail&toast={old['tone']}+톤으로+되돌렸어요", status_code=302)


@router.post("/scripts/{script_id}/delete")
def scripts_delete_history(script_id: str, product_id: str = Form(...)):
    """"이전에 생성한 톤" 목록에서 개별 항목을 삭제한다(사용자 피드백). 목록 자체가 현재
    최신 대본은 제외하고 보여주므로, 여기서 지울 수 있는 건 항상 과거분뿐이다. 되돌릴 수
    없어 프론트에서 confirm()을 거친다."""
    client = get_client()
    client.table("scripts").delete().eq("id", script_id).eq("product_id", product_id).execute()
    return RedirectResponse(f"/scripts?selected={product_id}&view=detail&toast=삭제했어요", status_code=302)


@router.post("/scripts/{script_id}/edit")
async def scripts_edit(request: Request, script_id: str):
    form = await request.form()
    product_id = form.get("product_id", "")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id는 필수입니다.")

    client = get_client()
    existing = client.table("scripts").select("*").eq("id", script_id).execute().data[0]
    script_json = existing["script_json"]
    fmt = get_format(script_json.get("tone"))
    # 서버가 조회한 이 대본 형식의 stage 목록만 신뢰한다 — 클라이언트가 임의 필드명을
    # 보내도 구조에 반영되지 않는다.
    script_json["structure"] = {key: form.get(f"stage__{key}", "") for key in fmt.stage_keys}
    # 씬별 화면연출(visual)/자막(caption) 편집 — 서버가 이미 알고 있는 seq만 신뢰해서 그
    # scene의 값만 갱신한다(사용자 피드백: 대본작성 시 씬별 화면 컨셉·자막을 직접 수정하고
    # 싶다 — 특히 상품선택 없이 빈 대본으로 시작한 경우 자막도 사람이 직접 채워야 한다).
    for scene in script_json.get("scenes") or []:
        visual_field = f"visual__{scene['seq']}"
        if visual_field in form:
            scene["visual"] = form.get(visual_field, scene.get("visual", ""))
        caption_field = f"caption__{scene['seq']}"
        if caption_field in form:
            scene["caption"] = form.get(caption_field, scene.get("caption", ""))
    client.table("scripts").update(
        {"script_json": script_json, "version": existing["version"] + 1}
    ).eq("id", script_id).execute()
    return RedirectResponse(
        f"/scripts?selected={product_id}&view=detail&toast=대본을+수정했어요", status_code=302
    )


@router.post("/scripts/{script_id}/approve")
def scripts_approve(request: Request, script_id: str, product_id: str = Form(...)):
    client = get_client()
    client.table("scripts").update({"approved": True}).eq("id", script_id).execute()
    # 승인 즉시 렌더를 큐잉하지 않는다 — 씬별 스틸컷/후킹 영상을 먼저 확인·재생성하고
    # 확정해야 실제 렌더가 시작된다(사용자 피드백: 대본작성과 미디어제작 사이에 프롬프트를
    # 확인하는 단계가 필요하다). 이 단계는 "프롬프트 확인" 탭(prompt_review 상태)에서
    # 진행하고, 확정(prompts_confirm)해야 비로소 script_approved로 넘어가 미디어제작
    # 탭에 노출된다 — 그전에 노출되면 확인/수정 과정에서 렌더 비용이 이중지출된다는
    # 피드백으로 분리했다.
    client.table("products").update({"status": "prompt_review"}).eq("id", product_id).execute()
    # 04_ui_spec.md: 다음 탭으로 강제 이동하지 않는다 — 토스트만 안내하고 대본작성 탭에
    # 머문다. 준비되면 사이드바의 "프롬프트 확인" 탭에서 언제든 다시 들어올 수 있다.
    return RedirectResponse(
        "/scripts?toast=승인+완료!+프롬프트+확인+탭에서+확인해주세요", status_code=302
    )


# --- 프롬프트 확인 탭 (씬별 프롬프트 확인/재생성, 후킹 포함 전체 씬) ---


@router.get("/prompts", response_class=HTMLResponse)
def prompts_tab(request: Request, selected: str | None = None, view: str = "list"):
    client = get_client()
    ctx = _base_ctx(request, client)
    products = (
        client.table("products")
        .select("*")
        .in_("status", PROMPT_STATUSES)
        .order("updated_at", desc=True)
        .execute()
        .data
    )
    selected_product = None
    latest_script = None
    if selected:
        found = [p for p in products if p["id"] == selected]
        if found:
            selected_product = found[0]
        else:
            # 확인 큐를 벗어난 상품(이미 확정했거나 이전 단계로 돌아간 경우)도 다시
            # 확인하러 돌아올 수 있으니 상태와 무관하게 직접 조회한다.
            other = client.table("products").select("*").eq("id", selected).execute().data
            selected_product = other[0] if other else None
        if selected_product:
            scripts = (
                client.table("scripts")
                .select("*")
                .eq("product_id", selected)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
            )
            latest_script = scripts[0] if scripts else None

    ctx.update({"active": "prompts", "counts": _counts(client), "products": products, "selected": selected_product})
    if not latest_script:
        ctx.update({"script": None, "view": view if selected_product else "list"})
        return templates.TemplateResponse("tab_prompts.html", ctx)

    script = latest_script
    script_json = script["script_json"]
    scenes = script_json["scenes"]
    first_seq = scenes[0]["seq"]
    fmt = get_format(script_json.get("tone"))

    jobs = (
        client.table("render_jobs")
        .select("*")
        .eq("script_id", script["id"])
        .eq("kind", "hook_preview")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    # target_seq별 최신 job만 남긴다(desc 정렬이라 먼저 만나는 게 최신) — null(=후킹)도 키로 취급.
    latest_job_by_seq: dict[int | None, dict] = {}
    for job in jobs:
        seq = job.get("target_seq")
        if seq not in latest_job_by_seq:
            latest_job_by_seq[seq] = job

    hook_preview_status = script.get("hook_preview_status")
    scene_previews = script.get("scene_preview_images") or {}

    # duration_sec이 나레이션 길이로 실제 도달 가능한 범위를 DURATION_MISMATCH_THRESHOLD_SEC
    # 이상 벗어나면 추천값을 같이 보여준다 — 그 이하 오차는 TTS 편차 수준이라 신경 쓰지
    # 않는다(사용자 피드백, 2026-08-19). 새로 저장되는 씬은 prompts_bulk_fill/prompts_generate가
    # resolve_scene_duration_sec()으로 미리 재설정하므로, 이 경고는 그 전에 저장된
    # 구버전 대본에서만 주로 뜬다.
    scene_cards = []
    for scene in scenes:
        seq = scene["seq"]
        is_hook = seq == first_seq
        job = latest_job_by_seq.get(None if is_hook else seq)
        in_progress = bool(job) and job["status"] not in ("done", "failed")
        if is_hook:
            status = hook_preview_status
            image_path = script.get("hook_preview_image_path")
            video_path = script.get("hook_preview_video_path")
        else:
            info = scene_previews.get(str(seq)) or {}
            status = info.get("status")
            image_path = info.get("image_path")
            video_path = None

        narration = scene.get("narration", "")
        duration_sec = scene.get("duration_sec")
        duration_warning = None
        if duration_sec and narration:
            predicted_sec = predict_scene_duration_sec(narration, duration_sec)
            gap_sec = predicted_sec - duration_sec
            if abs(gap_sec) >= DURATION_MISMATCH_THRESHOLD_SEC:
                duration_warning = {"recommended_sec": round(predicted_sec, 1), "gap_sec": round(gap_sec, 1)}

        scene_cards.append(
            {
                "seq": seq,
                "visual": scene.get("visual", ""),
                "narration": narration,
                "duration_sec": duration_sec,
                "duration_warning": duration_warning,
                "is_hook": is_hook,
                "is_deleted": bool(scene.get("deleted")),
                "is_pre_reveal": scene.get("stage") in fmt.pre_reveal_stages,
                "status": status,
                "image_path": image_path,
                "video_path": video_path,
                "in_progress": in_progress,
                "job_id": job["id"] if job else None,
                "error": job.get("error_message") if job and job["status"] == "failed" else None,
                # 후킹이 아직 확정 안 됐으면 다른 씬은 만들 수 없게 막는다 — 인물 참조를
                # 항상 후킹 이미지에서만 가져오기 위한 제약(일관성 유지, 사용자 피드백).
                "locked": (not is_hook) and hook_preview_status != "done",
            }
        )

    ctx.update(
        {
            "format": fmt,
            "scene_cards": scene_cards,
            "hook_preview_status": hook_preview_status,
            "hook_preview_in_progress": bool(latest_job_by_seq.get(None)) and latest_job_by_seq[None]["status"] not in ("done", "failed"),
            "view": view if selected_product else "list",
        }
    )
    ctx["script"] = script
    return templates.TemplateResponse("tab_prompts.html", ctx)


def _parse_bulk_prompt_paste(text: str) -> list[dict]:
    """스프레드시트에서 복사한 "단계,오디오,화면연출+자막" 표를 씬별 dict 목록으로 파싱한다.

    사용자 피드백(2026-08-17): 대본을 스프레드시트로 미리 써두고 한 번에 붙여넣고 싶다 —
    씬 하나하나 타이핑하는 대신, 표를 그대로 붙여넣으면 순서대로 각 씬(나레이션/화면연출/
    자막/길이)에 채워지길 바람. 구글시트 등에서 복사하면 탭 구분, 사람이 CSV 텍스트를
    직접 붙이면 콤마 구분이 되므로 첫 줄에 탭이 있는지로 구분자를 정한다. 따옴표로 감싼
    필드 안의 콤마/줄바꿈은 csv 모듈이 표준 규칙(이중따옴표 이스케이프)대로 처리한다.
    """
    text = text.strip()
    if not text:
        return []
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(cell.strip() for cell in row)]
    if rows and "단계" in rows[0][0]:
        rows = rows[1:]  # 헤더 행은 건너뛴다

    parsed = []
    for row in rows:
        if len(row) < 3:
            continue
        stage_col, narration_col, visual_caption_col = row[0], row[1], row[2]
        duration_match = re.search(r"(\d+)\s*초", stage_col)
        if "/자막" in visual_caption_col:
            visual, _, caption = visual_caption_col.partition("/자막")
            caption = caption.lstrip(": ").strip()
        else:
            visual, caption = visual_caption_col, ""
        parsed.append(
            {
                "stage_label": stage_col.strip(),
                "narration": narration_col.strip(),
                "visual": visual.strip(),
                "caption": caption,
                "duration_sec": int(duration_match.group(1)) if duration_match else None,
            }
        )
    return parsed


def _match_stage_key(stage_label: str, position_index: int, row_count: int, fmt) -> str:
    """붙여넣은 "단계" 칸 텍스트(예: "3. 해결 (10초)")를 이 형식의 실제 stage_key로 매핑한다.

    예전엔 행 순서를 stage_keys 개수에 맞춰 비율로만 분배했는데, 그러면 "해결"(상품이
    등장해야 하는 행)이 우연히 pre_reveal 단계(예: problem)에 매핑돼 실제 상품 참고사진이
    빠지고 가짜 상품이 그려지는 문제가 있었다(사용자 피드백, 2026-08-17). 이제 라벨
    텍스트로 먼저 매칭을 시도하고(CTA/후킹은 특별 취급, 나머지는 형식별 stage.label과
    비교), 실패하면 예전 방식(비율 분배)으로 안전하게 폴백한다.
    """
    normalized = re.sub(r"[\d.\s()초]+", "", stage_label)

    if "CTA" in stage_label.upper() or "마무리" in normalized or "구매유도" in normalized:
        return fmt.cta_stage
    if position_index == 0 or "후킹" in normalized or "hook" in stage_label.lower():
        return fmt.stage_keys[0]

    for stage in fmt.stages:
        stage_label_clean = stage.label.replace(" ", "")
        if stage_label_clean and (stage_label_clean in normalized or normalized[:2] == stage_label_clean[:2]):
            return stage.key

    return fmt.stage_keys[min(len(fmt.stage_keys) - 1, position_index * len(fmt.stage_keys) // row_count)]


@router.post("/prompts/{script_id}/bulk-fill")
def prompts_bulk_fill(request: Request, script_id: str, product_id: str = Form(...), pasted_text: str = Form(...)):
    """붙여넣은 표를 파싱해 씬 목록 자체를 통째로 새로 만든다.

    예전엔 기존 씬 개수와 붙여넣은 행 개수가 같아야만 채웠는데, AI가 원래 만들어둔 씬
    개수(예: 7개)와 사람이 붙여넣는 실제 대본의 단계 수(예: 5개)가 다른 경우가 흔해서
    (사용자 피드백, 2026-08-17) 항상 걸러졌다. 이제는 개수가 달라도 붙여넣은 행 수를
    그대로 새 씬 개수로 삼아 scenes를 완전히 교체한다. 각 씬의 stage는 이 대본 형식의
    stage_keys에 행 순서대로 고르게 분배한다(단계 수가 다르면 일부 stage는 씬이 없거나
    여러 씬이 몰릴 수 있음 — 이미 AI 생성 시에도 "한 단계를 씬 여러 개로 합쳐도 된다"는
    규칙이 있어 이 앱 전반에서 자연스러운 상황이다).

    스틸컷·후킹 영상은 다시 만들지 않는다 — 텍스트만 갱신하고, 실제 미리보기 생성은
    사람이 각 씬의 "미리보기 만들기" 버튼을 눌러야 시작되는 기존 흐름을 그대로 따른다.
    """
    client = get_client()
    script = client.table("scripts").select("*").eq("id", script_id).execute().data[0]
    script_json = script["script_json"]

    parsed_rows = _parse_bulk_prompt_paste(pasted_text)
    if not parsed_rows:
        return RedirectResponse(
            f"/prompts?selected={product_id}&view=detail&toast=붙여넣은+내용을+읽지+못했어요", status_code=302
        )

    fmt = get_format(script_json.get("tone"))
    stage_keys = fmt.stage_keys
    stage_order = {key: idx for idx, key in enumerate(stage_keys)}
    row_count = len(parsed_rows)

    product = client.table("products").select("image_urls").eq("id", product_id).execute().data
    image_count = len((product[0].get("image_urls") or []) if product else []) or 1

    new_scenes = []
    last_idx = -1
    for i, row in enumerate(parsed_rows):
        stage = _match_stage_key(row["stage_label"], i, row_count, fmt)
        idx = stage_order[stage]
        # 라벨 매칭이 형식의 원래 단계 순서를 거스르면(예: 4번째 행이 1번째 단계로 매칭)
        # 이전 씬보다 앞선 단계로는 되돌아가지 않게 고정한다 — pre_reveal 판정과 "전체
        # 컨셉" 카드가 순서를 전제로 하기 때문.
        if idx < last_idx:
            idx = last_idx
            stage = stage_keys[idx]
        last_idx = idx
        new_scenes.append(
            {
                "seq": i + 1,
                "stage": stage,
                "narration": row["narration"],
                "caption": row["caption"],
                "visual": row["visual"],
                "image_index": i % image_count,
                # 붙여넣은(또는 기본값 5초) 시간이 나레이션 길이와 크게 안 맞으면 대본
                # (나레이션) 기준으로 자동 재설정한다 — 그대로 두면 렌더링에서 속도 clamp로
                # 강제 조정되면서 실제 영상 길이가 설정값과 크게 어긋난다(사용자 피드백,
                # 2026-08-19, app/media/render.py의 resolve_scene_duration_sec 참고).
                "duration_sec": resolve_scene_duration_sec(row["narration"], row["duration_sec"] or 5),
            }
        )
    script_json["scenes"] = new_scenes

    # structure[key](전체 컨셉 카드에 보이는 stage별 나레이션)도 같은 stage를 가진 씬들의
    # 나레이션을 이어붙여 함께 갱신한다 — 안 하면 "전체 컨셉" 카드가 옛 텍스트로 남는다.
    script_json["structure"] = {
        key: " ".join(s["narration"] for s in new_scenes if s.get("stage") == key) for key in stage_keys
    }

    client.table("scripts").update(
        {
            "script_json": script_json,
            "version": script["version"] + 1,
            # 씬 구성 자체가 바뀌었으니 이전에 만들어둔 미리보기(이미지/후킹 영상)는 새
            # 내용과 안 맞는다 — 그대로 두면 엉뚱한 미리보기가 "완료"로 남아 헷갈린다.
            "hook_preview_image_path": None,
            "hook_preview_video_path": None,
            "hook_preview_status": None,
            "scene_preview_images": {},
        }
    ).eq("id", script_id).execute()
    return RedirectResponse(
        f"/prompts?selected={product_id}&view=detail&toast=씬+{row_count}개로+새로+채웠어요", status_code=302
    )


@router.post("/prompts/{script_id}/generate")
async def prompts_generate(request: Request, script_id: str):
    form = await request.form()
    product_id = form.get("product_id", "")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id는 필수입니다.")
    target_seq = form.get("target_seq")
    target_seq = int(target_seq) if target_seq else None

    client = get_client()
    existing = client.table("scripts").select("*").eq("id", script_id).execute().data[0]
    script_json = existing["script_json"]
    scenes = script_json["scenes"]
    first_seq = scenes[0]["seq"]
    is_hook = target_seq is None or target_seq == first_seq
    if not is_hook and existing.get("hook_preview_status") != "done":
        # 버튼은 이미 비활성화돼 있지만, API 레벨에서도 이중으로 막는다(일관성 유지 원칙).
        return RedirectResponse(
            f"/prompts?selected={product_id}&view=detail&toast=먼저+후킹+미리보기를+완료해주세요",
            status_code=302,
        )
    scene = scenes[0] if is_hook else next(s for s in scenes if s["seq"] == target_seq)
    # 화면 연출/나레이션만 수정 대상이다 — 사용자가 직접 확인 후 재생성해보고 싶은 텍스트가
    # 이 두 가지라(사용자와 논의해 범위 확정), 여기서만 갱신한다.
    if "visual" in form:
        scene["visual"] = form.get("visual", scene.get("visual", ""))
    if "narration" in form:
        scene["narration"] = form.get("narration", scene.get("narration", ""))
        # 나레이션 길이가 바뀌면 기존 duration_sec이 더 이상 안 맞을 수 있다 — 대본
        # (나레이션) 기준으로 자동 재설정한다(사용자 피드백, 2026-08-19).
        scene["duration_sec"] = resolve_scene_duration_sec(scene["narration"], scene.get("duration_sec"))

    update_payload = {"script_json": script_json, "version": existing["version"] + 1}
    if is_hook:
        update_payload["hook_preview_status"] = "generating"
    else:
        scene_previews = dict(existing.get("scene_preview_images") or {})
        scene_previews[str(scene["seq"])] = {**scene_previews.get(str(scene["seq"]), {}), "status": "generating"}
        update_payload["scene_preview_images"] = scene_previews
    client.table("scripts").update(update_payload).eq("id", script_id).execute()

    job_payload = {"script_id": script_id, "status": "queued", "kind": "hook_preview"}
    if not is_hook:
        job_payload["target_seq"] = scene["seq"]
    client.table("render_jobs").insert(job_payload).execute()
    return RedirectResponse(
        f"/prompts?selected={product_id}&view=detail&toast=미리보기를+만들고+있어요",
        status_code=302,
    )


@router.post("/prompts/{script_id}/scenes/{seq}/delete")
def prompts_delete_scene(request: Request, script_id: str, seq: int, product_id: str = Form(...)):
    """씬 하나를 "삭제됨"으로 표시한다 — 렌더링에서는 빠지지만 목록에는 흔적이 남는다.

    배열에서 완전히 지우지 않고 scene["deleted"]=True만 표시하는 소프트 삭제다 — 실수로
    지운 씬이 흔적도 없이 사라지면 안 된다는 요청(사용자 피드백, 2026-08-18). 렌더링
    (render_script)은 deleted 씬을 걸러내고, 프롬프트 확인 탭은 흐리게 표시만 한다.

    후킹(첫 씬)은 삭제할 수 없다 — 인물 참조 기준점이자 hook_preview_* 필드가 이 씬
    하나를 전제로 설계돼 있어서, 삭제를 허용하면 구조 전체가 깨진다. 활성 씬이 하나만
    남은 상태에서도 막는다(대본이 통째로 비어버리면 렌더링이 불가능하다).
    """
    client = get_client()
    script = client.table("scripts").select("*").eq("id", script_id).execute().data[0]
    script_json = script["script_json"]
    scenes = script_json.get("scenes") or []
    active_scenes = [s for s in scenes if not s.get("deleted")]

    if len(active_scenes) <= 1:
        return RedirectResponse(
            f"/prompts?selected={product_id}&view=detail&toast=씬이+하나뿐이라+삭제할+수+없어요", status_code=302
        )
    if scenes[0]["seq"] == seq:
        return RedirectResponse(
            f"/prompts?selected={product_id}&view=detail&toast=후킹(첫+장면)은+삭제할+수+없어요", status_code=302
        )

    for s in scenes:
        if s["seq"] == seq:
            s["deleted"] = True

    # structure(전체 컨셉 카드)도 갱신 — 삭제된 씬의 나레이션이 이어붙은 채 남지 않게.
    fmt = get_format(script_json.get("tone"))
    active_scenes = [s for s in scenes if not s.get("deleted")]
    script_json["structure"] = {
        key: " ".join(s["narration"] for s in active_scenes if s.get("stage") == key) for key in fmt.stage_keys
    }

    # 삭제한 씬의 개별 미리보기 기록도 함께 정리한다.
    scene_previews = dict(script.get("scene_preview_images") or {})
    scene_previews.pop(str(seq), None)

    client.table("scripts").update(
        {
            "script_json": script_json,
            "scene_preview_images": scene_previews,
            "version": script["version"] + 1,
        }
    ).eq("id", script_id).execute()
    return RedirectResponse(f"/prompts?selected={product_id}&view=detail&toast=씬을+삭제했어요", status_code=302)


@router.post("/prompts/{script_id}/confirm")
def prompts_confirm(request: Request, script_id: str, product_id: str = Form(...)):
    client = get_client()
    script = client.table("scripts").select("*").eq("id", script_id).execute().data[0]
    if script.get("hook_preview_status") != "done":
        return RedirectResponse(
            f"/prompts?selected={product_id}&view=detail&toast=먼저+후킹+미리보기를+완료해주세요",
            status_code=302,
        )
    client.table("render_jobs").insert({"script_id": script_id, "status": "queued", "kind": "full"}).execute()
    # 여기서 비로소 script_approved로 넘어가 미디어제작 탭에 노출된다 — 확인 전에
    # 노출되면 확인/수정 과정에서 렌더 비용이 이중지출된다는 피드백으로 이 시점까지 미룬다.
    client.table("products").update({"status": "script_approved"}).eq("id", product_id).execute()
    return RedirectResponse("/media?toast=완료!+미디어제작+탭에서+확인할+수+있어요", status_code=302)


# --- 미디어제작 탭 ---


# render_job.status는 worker.py의 실제 status_callback 값(queued/generating_images/
# generating_video/generating_audio/assembling/done/failed) 그대로다 — 사람이 읽을 만한
# 한글 라벨로 바꿔서 보여준다(사용자 피드백: 진행 상태를 더 명확히 보여달라).
_RENDER_STATUS_LABELS = {
    "queued": "대기 중",
    "claimed": "대기 중",
    "generating_images": "이미지 생성 중",
    "generating_video": "후킹 영상 생성 중",
    "generating_audio": "음성 생성 중",
    "assembling": "영상 합성 중",
    "done": "완료",
    "failed": "실패",
}


def _render_elapsed_label(created_at: str | None) -> str | None:
    """render_job.created_at부터 지금까지 경과 시간을 "N분 M초" 형태로 돌려준다.

    started_at 컬럼이 따로 없어 created_at(큐에 들어간 시각)을 기준으로 삼는다 —
    워커가 곧바로 집어가는 단일 워커 구조라 대기시간은 무시할 만하다. 렌더링이 오래
    걸리는데(무무스가드 사례 약 14분) 진행 상태 텍스트만 보여서는 얼마나 걸리는지 알 수
    없다는 피드백으로 추가했다.
    """
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    elapsed_sec = max(int((datetime.now(timezone.utc) - started).total_seconds()), 0)
    minutes, seconds = divmod(elapsed_sec, 60)
    return f"{minutes}분 {seconds}초"


@router.get("/media", response_class=HTMLResponse)
def media_tab(request: Request, selected: str | None = None, view: str = "list"):
    client = get_client()
    ctx = _base_ctx(request, client)
    products = (
        client.table("products")
        .select("*")
        .in_("status", MEDIA_STATUSES)
        .order("updated_at", desc=True)
        .execute()
        .data
    )
    selected_product = None
    render_job = None
    thumbnail = None
    disclosure_ok = deeplink_ok = False
    hook_patch_in_progress = False
    if selected:
        found = [p for p in products if p["id"] == selected]
        if found:
            selected_product = found[0]
        else:
            # 미디어제작 큐를 벗어난 상품(이전 단계로 돌아가 대본/이미지를 고치고 온 경우)도
            # 진행 상황을 다시 확인할 수 있어야 하니 상태와 무관하게 직접 조회한다.
            other = client.table("products").select("*").eq("id", selected).execute().data
            selected_product = other[0] if other else None
        if selected_product:
            scripts = client.table("scripts").select("*").eq("product_id", selected).order("created_at", desc=True).limit(1).execute().data
            script = scripts[0] if scripts else None
            if script:
                # kind='full'만 본다 — 후킹 확인/재생성용 render_jobs(kind='hook_preview'/
                # 'hook_patch')는 별도 추적용 행이라 이 탭에 노출되면 안 된다.
                jobs = (
                    client.table("render_jobs")
                    .select("*")
                    .eq("script_id", script["id"])
                    .eq("kind", "full")
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                )
                render_job = jobs[0] if jobs else None
                youtube = (script.get("script_json") or {}).get("youtube") or {}
                description = youtube.get("description", "") or ""
                disclosure_ok = description.rstrip().endswith(PARTNERS_DISCLOSURE)
                deeplink_ok = bool(selected_product.get("deeplink")) and (selected_product.get("deeplink") or "") in description
                if render_job:
                    thumbs = client.table("thumbnails").select("*").eq("render_job_id", render_job["id"]).order("created_at", desc=True).limit(1).execute().data
                    thumbnail = thumbs[0] if thumbs else None
                    patch_jobs = (
                        client.table("render_jobs")
                        .select("status")
                        .eq("source_render_job_id", render_job["id"])
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute()
                        .data
                    )
                    hook_patch_in_progress = bool(patch_jobs) and patch_jobs[0]["status"] not in ("done", "failed")
    render_in_progress = bool(render_job) and render_job["status"] not in ("done", "failed")
    ctx.update(
        {
            "active": "media",
            "counts": _counts(client),
            "products": products,
            "selected": selected_product,
            "render_job": render_job,
            "render_status_label": _RENDER_STATUS_LABELS.get(render_job["status"], render_job["status"]) if render_job else None,
            "render_elapsed_label": _render_elapsed_label(render_job["created_at"]) if render_in_progress else None,
            "hook_patch_in_progress": hook_patch_in_progress,
            "thumbnail": thumbnail,
            "disclosure_ok": disclosure_ok,
            "deeplink_ok": deeplink_ok,
            "view": view if selected_product else "list",
        }
    )
    return templates.TemplateResponse("tab_media.html", ctx)


@router.post("/media/{render_job_id}/regenerate-hook")
def media_regenerate_hook(
    request: Request,
    render_job_id: str,
    product_id: str = Form(...),
    target_seq: int | None = Form(None),
    visual: str | None = Form(None),
):
    client = get_client()
    job = client.table("render_jobs").select("*").eq("id", render_job_id).execute().data[0]
    if job["kind"] != "full" or job["status"] != "done":
        return RedirectResponse(
            f"/media?selected={product_id}&view=detail&toast=완료된+렌더에서만+장면을+다시+만들+수+있어요",
            status_code=302,
        )
    if visual is not None:
        # 캡션 편집기에서 화면연출 텍스트를 고쳐서 재생성을 누른 경우 — 나레이션은 여기서
        # 안 받는다(오디오·자막이 이미 확정돼 있어 바꾸면 타임라인이 깨진다, 계획 문서 참고).
        script = client.table("scripts").select("*").eq("id", job["script_id"]).execute().data[0]
        script_json = script["script_json"]
        seq = target_seq if target_seq is not None else script_json["scenes"][0]["seq"]
        for scene in script_json["scenes"]:
            if scene["seq"] == seq:
                scene["visual"] = visual
                break
        client.table("scripts").update(
            {"script_json": script_json, "version": script["version"] + 1}
        ).eq("id", job["script_id"]).execute()
    job_payload = {
        "script_id": job["script_id"],
        "status": "queued",
        "kind": "hook_patch",
        "source_render_job_id": render_job_id,
    }
    if target_seq is not None:
        job_payload["target_seq"] = target_seq
    client.table("render_jobs").insert(job_payload).execute()
    return RedirectResponse(
        f"/media?selected={product_id}&view=detail&toast=장면을+다시+만들고+있어요", status_code=302
    )


@router.post("/media/{render_job_id}/generate-thumbnail")
def media_generate_thumbnail(request: Request, render_job_id: str, product_id: str = Form(...)):
    client = get_client()
    job = client.table("render_jobs").select("*").eq("id", render_job_id).execute().data[0]
    script = client.table("scripts").select("*").eq("id", job["script_id"]).execute().data[0]
    product = client.table("products").select("*").eq("id", script["product_id"]).execute().data[0]

    # "empathy"로 하드코딩돼 있었는데, 형식(tone)마다 첫 단계 키가 다를 수 있어(예:
    # 기획천재발견형은 "hook", 썸쇼츠형은 "story_setup") 형식에서 실제 첫 단계 키를 조회한다.
    fmt = get_format(script["script_json"].get("tone"))
    hook_text = script["script_json"]["structure"][fmt.stage_keys[0]]
    work_dir = os.path.join("renders", render_job_id)
    # 후킹(첫 씬)에 실제 쓰인 스틸컷을 그대로 배경으로 써서, 썸네일 클릭 -> 영상 재생
    # 시작 화면이 시각적으로 이어지도록 한다 — 상품 원본 사진을 쓰면 썸네일과 실제
    # 영상 오프닝(문제 상황, 상품 미등장)이 서로 달라 어색했다(사용자 피드백).
    hook_still_path = os.path.join(work_dir, "scene_1.jpg")
    image_bytes = None
    if os.path.exists(hook_still_path):
        with open(hook_still_path, "rb") as f:
            image_bytes = f.read()

    os.makedirs(work_dir, exist_ok=True)
    output_path = os.path.join(work_dir, "thumbnail.jpg")
    generate_thumbnail(hook_text, image_bytes, product.get("category"), output_path)

    client.table("thumbnails").insert({"render_job_id": render_job_id, "image_path": output_path, "status": "done"}).execute()
    client.table("products").update({"status": "thumbnail_generated"}).eq("id", product_id).execute()
    return RedirectResponse(f"/media?selected={product_id}&view=detail&toast=썸네일을+다시+만들었어요", status_code=302)


@router.post("/media/{render_job_id}/queue-upload")
def media_queue_upload(request: Request, render_job_id: str, product_id: str = Form(...)):
    client = get_client()
    job = client.table("render_jobs").select("*").eq("id", render_job_id).execute().data[0]
    script = client.table("scripts").select("*").eq("id", job["script_id"]).execute().data[0]
    thumbs = client.table("thumbnails").select("*").eq("render_job_id", render_job_id).order("created_at", desc=True).limit(1).execute().data
    thumbnail = thumbs[0] if thumbs else None

    ready_at = (datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_HOLD_MINUTES)).isoformat()
    youtube_meta = (script["script_json"] or {}).get("youtube") or {}
    client.table("upload_queue").insert(
        {
            "render_job_id": render_job_id,
            "thumbnail_id": thumbnail["id"] if thumbnail else None,
            "youtube_title": youtube_meta.get("title"),
            "youtube_description": youtube_meta.get("description"),
            "ready_at": ready_at,
            "status": "pending_review",
        }
    ).execute()
    client.table("products").update({"status": "queued_for_upload"}).eq("id", product_id).execute()
    # 04_ui_spec.md: 다음 탭으로 강제 이동하지 않는다 — 토스트만 안내하고 현재(미디어제작) 탭에 머문다.
    return RedirectResponse("/media?toast=완료!+게시검토+탭에서+확인할+수+있어요", status_code=302)


# 씬 화면상 자막/후킹/상시CTA 드래그 편집 박스의 초기 위치 — 실제 렌더 위치를 픽셀 단위로
# 정확히 재현하진 않는다(줄바꿈 후 실제 텍스트 너비는 폰트 렌더링 전엔 알 수 없어서), 편집을
# 시작할 만한 근사치일 뿐이다. 사용자가 드래그해서 원하는 위치로 옮기는 게 에디터의 핵심이라
# 이 정도 근사로 충분하다고 판단했다.
def _default_caption_position(element_type: str, is_last: bool) -> tuple[int, int]:
    if element_type == "hook":
        return int(WIDTH * 0.07), 260
    if element_type == "sticky_cta":
        return int(WIDTH * 0.15), HEIGHT - 190 - 40
    # caption (마지막 씬은 CTA 스타일이라 더 위쪽에 작게 배치된다)
    bottom_margin = 390 if is_last else 360
    return int(WIDTH * 0.06), HEIGHT - bottom_margin - 120


def _default_font_size(element_type: str, is_last: bool) -> int:
    if element_type == "hook":
        return HOOK_FONTSIZE
    if element_type == "sticky_cta":
        return STICKY_CTA_FONTSIZE
    return CTA_CAPTION_FONTSIZE if is_last else CAPTION_FONTSIZE


def _default_text_color(element_type: str) -> str:
    if element_type == "hook":
        return "#ffffff"
    # caption/sticky_cta 기본 노란색(CAPTION_TEXT_COLOR="0xFFE600")과 동일한 값의
    # HTML 색상 입력용(#rrggbb) 표기.
    return "#ffe600"


# 색상 입력값 검증용 — <input type="color">가 보내는 형태(#rrggbb)만 허용한다.
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


# 캡션 편집기에서 허용하는 폰트 크기 범위 — 예전엔 소/중/대/특대 4단계 프리셋이었는데,
# 더 세밀하게 조절하고 싶다는 피드백으로 5px 단위 슬라이더로 바꿨다. 최소/최대는 그때의
# "소"/"특대" 값을 그대로 물려받는다.
FONT_SIZE_MIN = 32
FONT_SIZE_MAX = 100
FONT_SIZE_STEP = 5


def _clamp_font_size(font_size: int) -> int:
    return max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, font_size))


@router.get("/media/{render_job_id}/caption-editor", response_class=HTMLResponse)
def media_caption_editor(request: Request, render_job_id: str, product_id: str):
    client = get_client()
    job = client.table("render_jobs").select("*").eq("id", render_job_id).execute().data[0]
    script = client.table("scripts").select("*").eq("id", job["script_id"]).execute().data[0]
    scenes = script["script_json"]["scenes"]
    fmt = get_format(script["script_json"].get("tone"))
    elements_by_seq = resolve_scene_text_elements(scenes, fmt.pre_reveal_stages)
    overrides = job.get("caption_overrides") or {}

    scene_cards = []
    for scene in scenes:
        seq = scene["seq"]
        info = elements_by_seq.get(seq, {"texts": {}, "is_last": False})
        seq_overrides = overrides.get(str(seq)) or {}
        items = []
        for element_type, default_text in info["texts"].items():
            override = seq_overrides.get(element_type) or {}
            default_x, default_y = _default_caption_position(element_type, info["is_last"])
            font_size = override.get("font_size", _default_font_size(element_type, info["is_last"]))
            items.append(
                {
                    "type": element_type,
                    "text": override.get("text", default_text),
                    "x": override.get("x", default_x),
                    "y": override.get("y", default_y),
                    "font": override.get("font", DEFAULT_FONT_KEY),
                    "font_size": _clamp_font_size(font_size),
                    "text_color": override.get("text_color", _default_text_color(element_type)),
                    "bg_enabled": override.get("bg_enabled", element_type == "caption"),
                    "bg_color": override.get("bg_color", "#000000"),
                    "bg_opacity": override.get("bg_opacity", 0.55),
                }
            )
        scene_cards.append(
            {
                "seq": seq,
                "image_url": f"renders/{render_job_id}/scene_{seq}.jpg",
                "elements": items,
                "visual": scene.get("visual", ""),
                "is_hook": seq == scenes[0]["seq"],
            }
        )

    ctx = _base_ctx(request, client)
    ctx.update(
        {
            "active": "media",
            "render_job": job,
            "product_id": product_id,
            "scene_cards": scene_cards,
            "font_labels": FONT_LABELS,
            "font_size_min": FONT_SIZE_MIN,
            "font_size_max": FONT_SIZE_MAX,
            "font_size_step": FONT_SIZE_STEP,
            "canvas_width": WIDTH,
            "canvas_height": HEIGHT,
        }
    )
    return templates.TemplateResponse("caption_editor.html", ctx)


@router.post("/media/{render_job_id}/caption-editor/save")
def media_caption_editor_save(
    request: Request, render_job_id: str, product_id: str = Form(...), overrides_json: str = Form(...)
):
    try:
        overrides = json.loads(overrides_json)
    except json.JSONDecodeError:
        return RedirectResponse(
            f"/media/{render_job_id}/caption-editor?product_id={product_id}&toast=저장+실패%3A+잘못된+데이터",
            status_code=302,
        )

    # 최소 검증 — 허용된 요소 타입만, font는 레지스트리에 있는 것만 통과시킨다.
    if not isinstance(overrides, dict):
        overrides = {}
    for elements in overrides.values():
        if not isinstance(elements, dict):
            continue
        for element_type in list(elements.keys()):
            if element_type not in ("caption", "hook", "sticky_cta"):
                del elements[element_type]
                continue
            values = elements[element_type]
            if not isinstance(values, dict):
                continue
            if values.get("font") not in FONT_REGISTRY:
                values.pop("font", None)
            try:
                font_size = int(values.get("font_size"))
            except (TypeError, ValueError):
                values.pop("font_size", None)
            else:
                if FONT_SIZE_MIN <= font_size <= FONT_SIZE_MAX:
                    values["font_size"] = font_size
                else:
                    values.pop("font_size", None)
            if "text_color" in values and not HEX_COLOR_PATTERN.match(str(values.get("text_color"))):
                values.pop("text_color", None)

    client = get_client()
    job = client.table("render_jobs").select("*").eq("id", render_job_id).execute().data[0]
    script = client.table("scripts").select("*").eq("id", job["script_id"]).execute().data[0]
    work_dir = os.path.join("renders", render_job_id)

    if not job.get("base_video_path"):
        return RedirectResponse(
            f"/media?selected={product_id}&view=detail&toast=이+영상은+구버전+렌더라+캡션+편집을+지원하지+않아요",
            status_code=302,
        )

    try:
        # 재편집은 이 한 패스(오디오는 복사, 텍스트만 다시 그림)만 다시 돌리면 된다 —
        # Gemini/Veo/TTS를 다시 부를 필요가 없다.
        fmt = get_format(script["script_json"].get("tone"))
        final_path = recomposite_captions(
            job["base_video_path"],
            job["scene_timeline"],
            script["script_json"]["scenes"],
            script["script_json"]["disclosure"],
            resolve_font_path(),
            work_dir,
            os.path.join(work_dir, "final.mp4"),
            overrides=overrides,
            pre_reveal_stages=fmt.pre_reveal_stages,
        )
    except RenderError:
        return RedirectResponse(
            f"/media/{render_job_id}/caption-editor?product_id={product_id}&toast=재생성+실패%2C+다시+시도해주세요",
            status_code=302,
        )

    client.table("render_jobs").update({"caption_overrides": overrides, "output_path": final_path}).eq(
        "id", render_job_id
    ).execute()
    return RedirectResponse(
        f"/media/{render_job_id}/caption-editor?product_id={product_id}&toast=저장했어요", status_code=302
    )


# --- 게시검토 탭 ---


@router.get("/publish", response_class=HTMLResponse)
def publish_tab(request: Request, selected: str | None = None, view: str = "list"):
    client = get_client()
    ctx = _base_ctx(request, client)
    items = (
        client.table("upload_queue")
        .select("*")
        .in_("status", ["pending_review", "ready_to_publish", "published"])
        .order("created_at", desc=True)
        .execute()
        .data
    )
    selected_item = None
    render_job = None
    thumbnail = None
    remaining_minutes = 0
    product_id = None
    if selected:
        found = [i for i in items if i["id"] == selected]
        selected_item = found[0] if found else None
        if selected_item:
            render_job = client.table("render_jobs").select("*").eq("id", selected_item["render_job_id"]).execute().data[0]
            script = client.table("scripts").select("product_id").eq("id", render_job["script_id"]).execute().data
            product_id = script[0]["product_id"] if script else None
            if selected_item.get("thumbnail_id"):
                thumbs = client.table("thumbnails").select("*").eq("id", selected_item["thumbnail_id"]).execute().data
                thumbnail = thumbs[0] if thumbs else None
            if selected_item["status"] == "pending_review":
                ready_at = datetime.fromisoformat(selected_item["ready_at"].replace("Z", "+00:00"))
                remaining_minutes = max(int((ready_at - datetime.now(timezone.utc)).total_seconds() // 60), 0)
    ctx.update(
        {
            "active": "publish",
            "counts": _counts(client),
            "items": items,
            "selected": selected_item,
            "render_job": render_job,
            "thumbnail": thumbnail,
            "remaining_minutes": remaining_minutes,
            "product_id": product_id,
            "view": view if selected_item else "list",
        }
    )
    return templates.TemplateResponse("tab_publish.html", ctx)


@router.post("/publish/{upload_queue_id}/cancel")
def publish_cancel(request: Request, upload_queue_id: str):
    client = get_client()
    client.table("upload_queue").update({"status": "canceled"}).eq("id", upload_queue_id).execute()
    return RedirectResponse("/publish?toast=취소했어요", status_code=302)


@router.post("/publish/{upload_queue_id}/delete")
def delete_upload_queue_item(request: Request, upload_queue_id: str):
    """게시검토 탭 리스트 삭제 아이콘 — 다른 탭의 삭제와 동일하게 products부터 지운다.

    upload_queue 행만 지우면 products.status가 queued_for_upload로 남아, 어느 탭 목록에도
    안 걸리면서 대시보드 홈 '최근 프로젝트'(상태 무관하게 최근순으로 보여줌)에만 계속
    남는 고아 데이터가 되는 버그가 있었다. products를 지우면 FK cascade로
    scripts/render_jobs/upload_queue/thumbnails까지 함께 삭제된다.
    """
    client = get_client()
    item = client.table("upload_queue").select("*").eq("id", upload_queue_id).execute().data[0]
    render_job = client.table("render_jobs").select("script_id").eq("id", item["render_job_id"]).execute().data[0]
    script = client.table("scripts").select("product_id").eq("id", render_job["script_id"]).execute().data[0]
    client.table("products").delete().eq("id", script["product_id"]).execute()
    return RedirectResponse("/publish?toast=삭제했어요", status_code=302)


@router.post("/publish/{upload_queue_id}/publish")
def publish_now(request: Request, upload_queue_id: str):
    client = get_client()
    item = client.table("upload_queue").select("*").eq("id", upload_queue_id).execute().data[0]
    if item["status"] != "ready_to_publish":
        return RedirectResponse(f"/publish?selected={upload_queue_id}&view=detail&toast=아직+게시할+수+없는+상태예요", status_code=302)

    render_job = client.table("render_jobs").select("*").eq("id", item["render_job_id"]).execute().data[0]
    thumbnail_path = None
    if item.get("thumbnail_id"):
        thumbs = client.table("thumbnails").select("*").eq("id", item["thumbnail_id"]).execute().data
        thumbnail_path = thumbs[0]["image_path"] if thumbs else None

    try:
        video_id = publish_video(
            render_job["output_path"], item.get("youtube_title") or "", item.get("youtube_description") or "", thumbnail_path=thumbnail_path
        )
    except PublishError as exc:
        client.table("upload_queue").update({"status": "failed"}).eq("id", upload_queue_id).execute()
        return RedirectResponse(f"/publish?selected={upload_queue_id}&view=detail&toast=게시+실패:+{str(exc)[:60]}", status_code=302)

    client.table("upload_queue").update(
        {"status": "published", "youtube_video_id": video_id, "published_at": _now_iso()}
    ).eq("id", upload_queue_id).execute()
    return RedirectResponse("/publish?toast=업로드했어요!+유튜브+스튜디오에서+공개하세요+🎉", status_code=302)


# --- 정책 알림 목록 ---


@router.get("/policy-alerts", response_class=HTMLResponse)
def policy_alerts_page(request: Request):
    client = get_client()
    ctx = _base_ctx(request, client)
    alerts = client.table("policy_alerts").select("*, policy_snapshots(platform, policy_name, url)").order("detected_at", desc=True).execute().data
    ctx["alerts"] = alerts
    return templates.TemplateResponse("policy_alerts.html", ctx)


@router.post("/policy-alerts/{alert_id}/review")
def policy_alert_review(request: Request, alert_id: str, note: str = Form("")):
    client = get_client()
    client.table("policy_alerts").update(
        {"reviewed": True, "reviewed_at": _now_iso(), "note": note}
    ).eq("id", alert_id).execute()
    return RedirectResponse("/policy-alerts", status_code=302)
