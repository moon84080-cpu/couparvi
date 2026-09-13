"""Gemini(나노바나나) 이미지 생성 — 실사 상품 사진을 참고 이미지로 넣어서, 그 상품의 실제
형태·색상·로고는 유지하면서 씬의 화면 연출(visual)에 맞는 새 장면을 만든다.

정지 사진을 그대로 쓰면 판매 리스팅에 이미 박혀 있는 마케팅 문구가 함께 찍히고, 화면
연출과 실제 보유한 사진이 안 맞을 때가 많다는 문제(사용자 피드백)를 해결하기 위해 도입했다.
실제 제품 사진을 완전히 대체하지 않고 참고 이미지로 넘겨 외형을 최대한 유지하도록 한다.

단, 대본의 공감/문제 단계(아직 상품이 등장하면 안 되는 장면)는 상품 참고 이미지를 넘기지
않는다 — 참고 이미지로 넘기면 광고 대상 상품이 "문제 상황" 장면에 그대로 등장해버리는
모순이 생긴다(사용자 피드백). 어느 씬이 여기 해당하는지는 app/media/worker.py의
PRE_REVEAL_STAGES가 판단한다.

프롬프트 구조(사용자 피드백, 2026-08-18 — 상품 크기 부풀림/로고 변형/물체 공중부양 같은
실패 사례가 반복돼 지시를 역할·스타일·금지·장면 순으로 구조화하면 준수율이 오를 것으로
판단): [역할] 페르소나를 맨 앞에 두고, [스타일]과 [금지 요소]는 항상 고정 문구로
붙이며(그 중 금지 요소는 프롬프트 맨 끝에도 한 번 더 반복해 강조), 그 다음 상품/인물
처리 규칙과 이번 장면 설명이 이어진다.
"""

from __future__ import annotations

import base64
import time

import httpx

from app.config import GEMINI_API_KEY

MODEL = "gemini-2.5-flash-image"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

# 아트디렉터 페르소나 — 명령문만 나열하던 이전 프롬프트 대신 역할을 먼저 부여한다(사용자
# 피드백, 2026-08-18).
ART_DIRECTOR_PERSONA = (
    "[역할] 당신은 한국 이커머스 숏폼(쇼츠) 광고를 전문으로 만드는 3D 애니메이션 아트디렉터다. "
    "실제 상품 참고 이미지를 바탕으로, 아래 규칙을 모두 지키면서 요청된 장면 딱 하나만 정확히 생성한다."
)

# 모든 씬에 항상 적용되는 전체 분위기 고정 문구(사용자 피드백, 2026-08-18) — 프롬프트
# 시작 부분에 붙인다. "사실적인 사진 스타일" 대신 3D 애니메이션 스타일로 전환하되, 상품의
# 실제 형태·색상·로고 유지 규칙(아래 product_instruction)은 그대로 유지한다.
STYLE_FIX = (
    "[스타일 — 모든 장면에 항상 적용] 3D 애니메이션 스타일, 밝은 낮 시간대의 자연광, "
    "따뜻하고 부드러운 조명, 깔끔하고 심미적인 인테리어."
)

# 주방가전 등 특정 카테고리에서만 붙이는 배경 고정 문구 — "Cozy Korean modern kitchen"을
# 모든 카테고리에 강제하면 캠핑/야외 등 실제 장면 연출(visual)과 충돌한다는 걸 실측 리뷰
# 캡처로 확인해(사용자 피드백, 2026-08-18) 카테고리 매칭일 때만 적용한다(app/media/graphics.py의
# 카테고리 키워드 매칭과 같은 패턴, 초기값이며 운영하며 보강).
KITCHEN_BACKDROP_FIX = "포근한 한국식 모던 주방을 배경으로 한다."
KITCHEN_CATEGORY_KEYWORDS = ("주방", "조리", "키친", "쿠킹")

# 공포/고어/피/어둡고 무서운 톤/과장된 액체/기괴한 아티팩트 — 기존 "물체 공중부양 금지" 같은
# 물리 법칙 위반 방지 규칙과는 별개로, 심미적으로 부적절한 톤 자체를 막는 규칙이 없었다
# (사용자 피드백, 2026-08-18). 프롬프트 앞뒤에 반복해 강조한다.
NEGATIVE_FIX = (
    "[금지 요소 — 어떤 장면에도 절대 등장 금지] 공포·호러 분위기, 고어, 피, 어둡고 무서운 톤, "
    "과장되거나 기괴하게 튀는 액체 표현, 형태가 깨진 기괴한 아티팩트."
)

# 대본 LLM이 scenes[].situation으로 태깅한 5개 값(app/script/formats.py의 SITUATION_CHOICES,
# 형식/tone과 무관하게 항상 같은 값)을 이미지 스타일 프리셋으로 매핑한다(사용자 피드백,
# 2026-08-18) — 씬마다 매번 새로 분석하는 대신, 정해진 5개 상황에 미리 정한 스타일만
# 갖다 붙이는 방식이라 비용/지연시간 추가 없이(별도 LLM 호출 없이) 적용된다. situation이
# 없으면(구버전 대본 등) 프리셋 없이 STYLE_FIX만 적용된다.
SITUATION_STYLE_PRESETS: dict[str, str] = {
    "situation_hook": "긴장감이나 답답함이 느껴지는 클로즈업 위주 구도로, 표정과 디테일에 집중해서 그린다.",
    "situation_analysis": "정보 전달에 집중한 깔끔한 구도 — 체크리스트나 비교표를 보는 듯한 정돈된 배치, 과장 없는 차분한 톤.",
    "situation_usage": "상품을 실제로 사용하는 손과 동작이 선명하게 보이는 시연 구도로 그린다.",
    "situation_conclusion": "만족스럽고 편안한 표정으로 마무리하는, 여유롭고 따뜻한 톤의 구도로 그린다.",
    "situation_cta": "상품이 화면 중앙에서 선명하게 부각되는 하이라이트 구도로 그린다.",
}


class ImageGenerationError(RuntimeError):
    """이미지 생성 실패(API 오류, 응답에 이미지 없음 등)를 감싸는 명확한 예외."""


def _is_kitchen_category(category: str | None) -> bool:
    return bool(category) and any(keyword in category for keyword in KITCHEN_CATEGORY_KEYWORDS)


def _build_prompt(
    product_name: str,
    visual: str,
    narration: str,
    has_product_reference: bool,
    has_character_reference: bool,
    category: str | None = None,
    situation: str | None = None,
) -> str:
    if has_product_reference:
        product_instruction = (
            f"참고 이미지 중 첫 번째는 실제 상품 '{product_name}'의 사진이다. 이 상품의 실제 형태·색상·"
            f"디자인·로고는 절대 바꾸지 말고 그대로 유지하면서, 아래 장면 연출에 맞게 배경과 "
            f"상황만 새로 구성한 이미지를 세로 방향(9:16, 쇼츠용)으로 생성해줘. 상품의 실제 크기"
            f"(사람의 손·몸과 비교했을 때 상대적 크기)도 참고 이미지에 나온 비율 그대로 유지해줘 — "
            f"실제보다 크거나 작게 부풀려서 그리지 마. 상품이 인물의 몸이나 손, 다른 물체에 가려지지 "
            f"않고 화면에서 명확하게 잘 보이는 구도로 그려라."
        )
    else:
        # 대본의 공감/문제 단계(pre-reveal)는 아직 상품이 등장하면 안 되는 장면이다 — 상품을
        # 참고 이미지로 넘기면 Gemini가 그 상품을 그대로 그려버려서, 광고할 상품이 "문제
        # 상황"에 나오는 모순이 생긴다(사용자 피드백). 그래서 이 단계는 참고 이미지 없이
        # 순수 텍스트만으로 생성한다.
        product_instruction = (
            f"이 장면에는 광고 대상 상품 '{product_name}'이 아직 등장하면 안 된다 — 이 장면은 그 "
            f"상품이 해결해주기 이전의 문제/불편 상황을 보여주는 장면이다. 이 상품과는 무관하게, "
            f"아래 장면 연출에 어울리는 상황(예: 크고 투박한 기존 제품, 불편한 상황)을 자유롭게 "
            f"그려도 된다. 기존 제품(예: 오븐 등 가전)을 그릴 때는 반드시 하나의 명확하고 "
            f"일관된 형태로만 그려라 — 서로 다른 제품 두 대가 겹쳐 있거나 위아래로 쌓여 있는 "
            f"것처럼 보이는 구도는 절대 금지다. 세로 방향(9:16, 쇼츠용)으로 생성해줘."
        )

    if has_character_reference:
        character_instruction = (
            f"\n\n지금 만들 장면: {visual}\n\n"
            f"이 장면을 위 설명 그대로, 완전히 새로운 카메라 앵글·거리·구도·인물의 자세와 "
            f"동작으로 그려라. 두 번째 참고 이미지는 오직 '이 사람의 얼굴·헤어스타일·체형·"
            f"옷차림이 어떻게 생겼는지'를 알려주는 인물 자료일 뿐이다 — 그 사진 속 자세나 "
            f"구도, 카메라 위치를 이번 장면에 재사용하면 실패로 간주한다. 예를 들어 두 번째 "
            f"참고 이미지에서 인물이 서서 손을 괴고 있었다면, 이번 장면에서는 완전히 다른 "
            f"동작(예: 걸어오는 모습, 앉아 있는 모습, 손을 뻗는 모습, 클로즈업 등 위 장면 "
            f"설명에 맞는 동작)으로 바꿔 그려야 한다.\n\n"
            f"단, 성별·인종·피부톤·얼굴형·이목구비·나이대·머리색은 절대로 바뀌면 안 된다 — "
            f"이건 '비슷한 분위기의 다른 사람'이 아니라 '정확히 이 사람'이어야 한다. 예를 들어 "
            f"두 번째 참고 이미지 속 인물이 여성이면 이번 장면의 인물도 반드시 여성이어야 하고, "
            f"남성이면 반드시 남성이어야 한다 — 마찬가지로 서양인이면 이번 장면도 서양인, "
            f"동양인이면 동양인이어야 한다. 성별이나 인종이 바뀌는 것은 자세나 구도가 똑같은 "
            f"것보다 훨씬 심각한 실패로 간주한다. 사람이 꼭 필요하지 않은 장면(예: 제품 "
            f"클로즈업)이라면 인물 없이 생성해도 된다.\n\n"
            f"헤어스타일·체형·옷차림도 기본적으로는 참고 이미지와 같게 유지해라 — 위 '지금 "
            f"만들 장면' 설명에 다른 헤어스타일/체형/복장이 명시적으로 적혀 있을 때만 바꿔라. "
            f"참고 이미지 속 옷이 얼룩지거나 더러워져 있어도, 옷의 종류·색상·스타일만 그대로 "
            f"유지하고 얼룩·오염·핏자국처럼 보이는 자국은 절대 옮기지 마 — 위 '지금 만들 장면' "
            f"설명에 그런 자국이 명시적으로 적혀 있을 때만 그려라. 특정 장면에서 우연히 생긴 "
            f"자국을 이유 없이 다른 장면까지 계속 이어붙이면 안 된다."
        )
    else:
        character_instruction = f"\n\n지금 만들 장면: {visual}"

    style_block = STYLE_FIX
    if _is_kitchen_category(category):
        style_block = f"{style_block} {KITCHEN_BACKDROP_FIX}"
    situation_preset = SITUATION_STYLE_PRESETS.get(situation or "")
    if situation_preset:
        style_block = f"{style_block} {situation_preset}"

    return (
        f"{ART_DIRECTOR_PERSONA}\n\n"
        f"{style_block}\n\n"
        f"{NEGATIVE_FIX}\n\n"
        f"{product_instruction}"
        f"{character_instruction}\n\n"
        f"장면 내레이션(맥락 참고용, 화면에 이 문장을 글자로 넣지 마): {narration}\n\n"
        f"사진 안에 어떤 텍스트·워터마크·로고 문구도 넣지 마. "
        f"장면 연출이나 내레이션에서 요구하지 않는 한, 태블릿·휴대폰·서류 같은 불필요한 소품을 "
        f"들고 있는 모습은 넣지 마 — 인물은 자연스러운 자세로만 그려라. "
        f"모든 물체는 중력에 맞게 표면(바닥, 조리대, 선반 등) 위에 자연스럽게 놓여 있어야 "
        f"한다 — 프라이팬이나 그릇처럼 받침 없이 공중에 떠 있는 것처럼 보이는 물체를 절대 "
        f"그리지 마.\n\n"
        f"{NEGATIVE_FIX}"
    )


def generate_scene_image(
    visual: str,
    narration: str,
    product_name: str,
    product_reference: tuple[bytes, str] | None = None,
    character_reference: tuple[bytes, str] | None = None,
    client: httpx.Client | None = None,
    category: str | None = None,
    situation: str | None = None,
) -> tuple[bytes, str]:
    """생성된 (이미지 바이트, mime type)을 반환한다. 실패하면 ImageGenerationError.

    product_reference를 생략하면(대본의 공감/문제 단계 등 아직 상품이 나오면 안 되는 장면)
    참고 이미지 없이 순수 텍스트만으로 생성한다 — 그래야 광고 대상 상품이 "문제 상황"
    장면에 잘못 등장하지 않는다.

    character_reference를 넘기면(이전 씬에서 생성된 이미지) 그 인물과 동일한 외모를
    유지하도록 참고 이미지로 함께 보낸다 — 씬마다 독립적으로 생성하면 등장인물이 매번
    바뀌는 문제가 있어서, 영상 하나 안에서는 같은 인물이 이어지도록 유도한다.

    category는 상품 카테고리 원문(예: "주방가전") — 키워드가 매칭되면 KITCHEN_BACKDROP_FIX
    배경을 추가로 붙인다(app/media/graphics.py의 카테고리 키워드 매칭과 같은 패턴).

    situation은 scenes[].situation 값(app/script/formats.py의 SITUATION_CHOICES) — 형식(tone)과
    무관하게 항상 같은 5개 값 중 하나이며, SITUATION_STYLE_PRESETS로 씬 상황에 맞는 스타일을
    추가한다. 구버전 대본처럼 값이 없으면 프리셋 없이 STYLE_FIX만 적용된다.
    """
    if not GEMINI_API_KEY:
        raise ImageGenerationError("GEMINI_API_KEY가 설정되지 않았습니다.")

    parts = [
        {
            "text": _build_prompt(
                product_name,
                visual,
                narration,
                product_reference is not None,
                character_reference is not None,
                category=category,
                situation=situation,
            )
        }
    ]
    if product_reference is not None:
        product_bytes, product_media_type = product_reference
        parts.append(
            {
                "inlineData": {
                    "mimeType": product_media_type,
                    "data": base64.standard_b64encode(product_bytes).decode("utf-8"),
                }
            }
        )
    if character_reference is not None:
        char_bytes, char_media_type = character_reference
        parts.append(
            {
                "inlineData": {
                    "mimeType": char_media_type,
                    "data": base64.standard_b64encode(char_bytes).decode("utf-8"),
                }
            }
        )

    # 이미지가 9:16이 아니게 나오면 render.py가 위아래를 블러 배경으로 채워야 해서(레터박스
    # 회피용) 프레임을 다 못 채운다 — 프롬프트 문구만으로는 비율이 들쭉날쭉해서 API 설정으로
    # 명시한다.
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"imageConfig": {"aspectRatio": "9:16"}},
    }

    active_client = client or httpx.Client(timeout=60.0)
    last_error: Exception | None = None
    # AGENTS.md 기본 컨벤션(재시도 1회)보다 한 번 더 시도한다 — 실측(사용자 피드백)으로
    # 재시도 1회까지 연속 실패해 원본 리스팅 사진으로 폴백된 씬이 나온 걸 확인했다. 실패
    # 시 폴백되는 원본 사진은 다른 씬들과 스타일이 달라(흰 배경 스튜디오 사진) 영상 전체의
    # 일관성이 깨지므로, 이미지 생성은 영상 생성(Veo)과 달리 비용이 작아 시도를 늘릴 만하다.
    for attempt in range(3):
        try:
            response = active_client.post(API_URL, params={"key": GEMINI_API_KEY}, json=body)
            if response.status_code >= 400:
                raise ImageGenerationError(
                    f"Gemini 이미지 생성 실패 (status={response.status_code}): {response.text[:500]}"
                )
            data = response.json()
            candidates = data.get("candidates") or []
            response_parts = (candidates[0].get("content") or {}).get("parts") if candidates else None
            for part in response_parts or []:
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    image_bytes = base64.standard_b64decode(inline["data"])
                    return image_bytes, inline.get("mimeType", "image/png")
            raise ImageGenerationError(f"응답에 이미지 데이터가 없습니다: {str(data)[:500]}")
        except (httpx.HTTPError, ImageGenerationError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5)
                continue

    raise ImageGenerationError(f"Gemini 이미지 생성 실패: {last_error}") from last_error
