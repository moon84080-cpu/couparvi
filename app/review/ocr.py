"""리뷰 스크린샷에서 텍스트를 추출한다 — 이미지 인식(vision) 모델을 사용한다.

쿠팡 크롤링과 무관하다 — 사용자가 직접 캡처해 업로드한 이미지 파일을 읽을 뿐이다.

세 공급자를 지원한다(app.config.OCR_PROVIDER로 선택, TTS_PROVIDER와 같은 패턴):
- "gemini"(기본값): Gemini vision. 실제 리뷰 캡처로 claude와 비교 실측한 결과(사용자
  피드백, 2026-08-18) 속도·정확도·비용 모두 압도적으로 우세해 기본값으로 승격했다 —
  이미 있는 GEMINI_API_KEY로 바로 동작한다.
- "claude"(예전 기본값): Claude vision. 이미 있는 ANTHROPIC_API_KEY로 바로 동작한다.
- "openai": GPT-4o-mini vision. 비용 개선 목적으로 추가했다(사용자 피드백, 2026-08-18)
  — OPENAI_API_KEY가 필요하다. 단, 실제 인식률은 아직 검증하지 않았다.
"""

from __future__ import annotations

import base64
import io
import time

import anthropic
import httpx
from PIL import Image, ImageFilter

from app.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, OCR_PROVIDER, OPENAI_API_KEY

CLAUDE_MODEL = "claude-sonnet-4-5"
# gpt-4o보다 훨씬 싸면서도 vision을 지원한다 — OCR_PROVIDER=openai로 바꾸는 주된 이유가
# 비용이라(사용자 피드백, 2026-08-18) gpt-4o-mini를 기본값으로 쓴다. 필터링 지시(리뷰
# 본문만 추출)는 프롬프트가 그대로 하므로 모델을 가볍게 바꿔도 그 능력 자체는 유지된다.
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# claude/openai보다도 토큰 단가가 더 싸서(사용자 피드백, 2026-08-18) 인식률 검증용으로
# 추가한다 — app/media/image_generator.py와 같은 Gemini generateContent 엔드포인트.
# gemini-2.5-flash-lite는 신규 사용자에게 막혀 있어(2026-08-18 실측, API 404 응답의 안내
# 문구를 따름) 3.5 세대로 바로 지정한다.
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_CHAT_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# 작은 스크린샷(특히 좁은 배너형 캡처)은 실제 사용해보니 글자 오독이 잦았다 — 짧은 변이
# 이 값보다 작으면 확대해서 보낸다. 실측: 세로 40px 리뷰 캡처에서 여러 단어를 잘못 읽던
# 문제가, 확대 후엔 거의 완벽하게 교정됐다(체감상 vision 모델이 작은 글자를 실제로 읽기보다
# 비슷한 모양으로 추측해버리는 것으로 보임).
OCR_MIN_DIMENSION_PX = 800
OCR_MAX_UPSCALE = 4

# 확대해도 여전히 인식률이 낮다는 피드백(사용자) — 캡처 특유의 리사이즈/압축 흐림은
# 크기와 무관하게 발생하므로, 이미 충분히 큰 이미지에도 약한 선명화(unsharp mask)를
# 항상 적용한다. radius/percent는 자글자글한 노이즈를 만들지 않는 선에서 보수적으로 잡았다.
OCR_SHARPEN_RADIUS = 1.5
OCR_SHARPEN_PERCENT = 60
OCR_SHARPEN_THRESHOLD = 2

SYSTEM_PROMPT = """너는 상품 후기 스크린샷에서 텍스트를 추출하는 AI 직원이다.
이미지 안에 보이는 리뷰 본문 텍스트를 있는 그대로 옮겨 적어라 (요약하거나 각색하지 않는다).
여러 리뷰가 보이면 리뷰별로 줄바꿈해서 전부 옮겨 적는다.
별점, 작성일, 작성자 닉네임 같은 부가 정보는 제외하고 리뷰 본문 텍스트만 추출한다.
다른 설명 없이 추출한 텍스트만 출력한다. 리뷰 텍스트가 보이지 않으면 빈 문자열만 출력한다."""


class ReviewOcrError(RuntimeError):
    """스크린샷 텍스트 추출 실패(인증 오류 포함)를 감싸는 명확한 예외."""


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise ReviewOcrError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


USER_TEXT_PROMPT = "이 스크린샷에서 리뷰 텍스트를 추출해줘."


def _preprocess_for_ocr(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """OCR 정확도를 높이기 위해 확대 + 선명화를 적용한다.

    짧은 변이 작으면 OCR_MIN_DIMENSION_PX까지 확대하고, 크기와 무관하게 약한 선명화를
    항상 적용한다(화면 캡처 특유의 리사이즈/압축 흐림은 이미 큰 이미지에도 있을 수 있다).
    이미지를 열 수 없으면(지원 안 하는 포맷 등) 원본을 그대로 돌려주고 Claude에게 맡긴다.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return image_bytes, media_type

    image = image.convert("RGB")
    shorter_side = min(image.width, image.height)
    if 0 < shorter_side < OCR_MIN_DIMENSION_PX:
        scale = min(OCR_MIN_DIMENSION_PX / shorter_side, OCR_MAX_UPSCALE)
        new_size = (round(image.width * scale), round(image.height * scale))
        image = image.resize(new_size, Image.LANCZOS)

    image = image.filter(
        ImageFilter.UnsharpMask(radius=OCR_SHARPEN_RADIUS, percent=OCR_SHARPEN_PERCENT, threshold=OCR_SHARPEN_THRESHOLD)
    )

    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue(), "image/png"


def _extract_with_claude(image_bytes: bytes, media_type: str, b64: str, client: anthropic.Anthropic | None) -> str:
    active_client = client or _client()

    last_error: Exception | None = None
    for attempt in range(2):  # 최초 시도 + 재시도 1회 (AGENTS.md 코딩 컨벤션)
        try:
            # 길고 상세한 리뷰(섹션 제목, 이모지, 여러 문단)는 예전 2048 한도에서 중간에
            # 잘릴 수 있었다 — 사용자가 "인식 실패"라 보고한 리뷰가 실제로는 텍스트 자체는
            # 또렷했고 분량만 많았던 사례가 있어 여유 있게 올렸다.
            message = active_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64},
                            },
                            {"type": "text", "text": USER_TEXT_PROMPT},
                        ],
                    }
                ],
            )
            return message.content[0].text.strip()
        except anthropic.APIError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
                continue

    raise ReviewOcrError(f"스크린샷 텍스트 추출 실패(Claude): {last_error}") from last_error


def _extract_with_openai(media_type: str, b64: str, client: httpx.Client | None) -> str:
    if not OPENAI_API_KEY:
        raise ReviewOcrError("OPENAI_API_KEY가 설정되지 않았습니다.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": USER_TEXT_PROMPT},
                ],
            },
        ],
    }

    last_error: Exception | None = None
    for attempt in range(2):  # 최초 시도 + 재시도 1회 (AGENTS.md 코딩 컨벤션)
        try:
            if client is not None:
                response = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
            else:
                with httpx.Client(timeout=60.0) as default_client:
                    response = default_client.post(OPENAI_CHAT_URL, headers=headers, json=body)
            if response.status_code >= 400:
                raise ReviewOcrError(f"OpenAI 요청 실패 (status={response.status_code}): {response.text[:300]}")
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, ReviewOcrError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
                continue

    raise ReviewOcrError(f"스크린샷 텍스트 추출 실패(OpenAI): {last_error}") from last_error


def _extract_with_gemini(media_type: str, b64: str, client: httpx.Client | None) -> str:
    if not GEMINI_API_KEY:
        raise ReviewOcrError("GEMINI_API_KEY가 설정되지 않았습니다.")

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {
                "parts": [
                    {"inlineData": {"mimeType": media_type, "data": b64}},
                    {"text": USER_TEXT_PROMPT},
                ]
            }
        ],
    }

    last_error: Exception | None = None
    for attempt in range(2):  # 최초 시도 + 재시도 1회 (AGENTS.md 코딩 컨벤션)
        try:
            if client is not None:
                response = client.post(GEMINI_CHAT_URL, params={"key": GEMINI_API_KEY}, json=body)
            else:
                with httpx.Client(timeout=60.0) as default_client:
                    response = default_client.post(GEMINI_CHAT_URL, params={"key": GEMINI_API_KEY}, json=body)
            if response.status_code >= 400:
                raise ReviewOcrError(f"Gemini 요청 실패 (status={response.status_code}): {response.text[:300]}")
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise ReviewOcrError(f"응답에 candidates가 없습니다(안전 필터 차단 등): {str(data)[:300]}")
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(part.get("text", "") for part in parts)
            return text.strip()
        except (httpx.HTTPError, ReviewOcrError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
                continue

    raise ReviewOcrError(f"스크린샷 텍스트 추출 실패(Gemini): {last_error}") from last_error


def extract_review_text(
    image_bytes: bytes,
    media_type: str,
    client: anthropic.Anthropic | httpx.Client | None = None,
    provider: str | None = None,
) -> str:
    """provider가 없으면 app.config.OCR_PROVIDER를 따른다.

    client의 실제 타입은 provider에 따라 다르다 — "claude"면 anthropic.Anthropic,
    "openai"/"gemini"면 httpx.Client(테스트에서 가짜 클라이언트를 주입할 때 참고).
    """
    image_bytes, media_type = _preprocess_for_ocr(image_bytes, media_type)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    active_provider = provider or OCR_PROVIDER
    if active_provider == "claude":
        return _extract_with_claude(image_bytes, media_type, b64, client)
    if active_provider == "openai":
        return _extract_with_openai(media_type, b64, client)
    if active_provider == "gemini":
        return _extract_with_gemini(media_type, b64, client)
    raise ReviewOcrError(f"알 수 없는 OCR_PROVIDER: {active_provider!r} (claude, openai, gemini만 지원)")
