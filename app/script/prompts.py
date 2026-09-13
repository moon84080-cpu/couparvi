"""3호 직원 — 대본 프롬프트: 구조(형식별 가변) + 톤(형식별 문체) 이중 프롬프트.

v2.4부터 구조도 형식(tone)마다 달라질 수 있다 — 실제 단계 시퀀스/문체는
app/script/formats.py의 SCRIPT_FORMATS 레지스트리가 정의한다.

docs/03_interfaces.md 4번 스키마와 docs/00_project_overview.md "대본 설계" 절 기준.
"""

from __future__ import annotations

import json

from app.config import DEFAULT_TARGET_PERSONA, PARTNERS_DISCLOSURE
from app.script.formats import SITUATION_CHOICES, SITUATION_DESCRIPTIONS, ScriptFormat, all_known_tones, get_format

EDUCATIONAL_NOTE_INSTRUCTION_TEMPLATE = """이 상품은 needs_education=true이므로 {stage_label} 단계 직후에 삽입할
educational_note를 함께 생성한다. 규칙:
- 널리 합의된 상식 수준의 사실만 사용한다 (예: "자외선은 피부 노화·화상을 유발할 수 있다"는 가능, 특정 질병의
  진단·치료 효과를 암시하는 표현은 불가).
- 의학적 조언·진단처럼 들리는 표현을 쓰지 않는다 ("~하면 안 걸립니다", "치료됩니다" 등 금지).
- 과장·확정적 인과 표현 대신 "~일 수 있어요", "~로 알려져 있어요" 같은 절제된 어투를 쓴다.
educational_note는 {{"included": true, "text": "..."}} 형태로 채운다."""

NO_EDUCATIONAL_NOTE_INSTRUCTION = (
    'educational_note는 이 상품에 해당하지 않으므로 {"included": false, "text": ""}로 둔다.'
)

# v2.2: 가격은 표기하지 않고 "본문 확인"으로 유도, 고지문구는 narration이 아니라
# youtube.description 마지막 줄에만 넣는다 (사용자 피드백 반영).
PRICE_INSTRUCTION = """가격은 narration/구조(structure)/youtube.title/youtube.description 어디에도
숫자로 표기하지 않는다 (예: "18,900원", "2만원대" 금지 — 가격은 변동될 수 있고 딥링크를 눌러야 최신가를
확인하게 유도하는 편이 낫다). 구매를 유도할 때는 "제품정보(가격 포함)는 본문에 있어요, 확인해보세요"처럼
"본문"/"더보기"/"설명란" 중 하나를 반드시 포함한 문구로 마무리한다."""

DISCLOSURE_PLACEMENT_INSTRUCTION = f"""고지문구는 narration(structure의 모든 필드, scenes[].narration)에는
절대 넣지 않는다 — 음성으로 낭독하지 않는다. youtube.description의 맨 마지막 줄에만 아래 문구를 정확히
그대로 넣는다: "{PARTNERS_DISCLOSURE}"
description 앞부분에는 상품 매력 요약과 구매 링크 안내를 넣는다: user 메시지의 product.deeplink 값이 있으면
그 URL을 그대로 포함하고, 없으면 "구매 링크는 본문 더보기에서 확인해 주세요"라는 안내문으로 대체한다.
그 다음 줄(빈 줄로 구분)에 위 고지문구를 붙인다."""


def _build_output_schema_example(needs_education: bool, fmt: ScriptFormat) -> dict:
    """needs_education/형식(fmt)에 맞는 구조·educational_note 예시를 보여준다.

    항상 included=false 예시만 주면 모델이 지시문보다 예시를 따라가
    needs_education=true인 상품에도 educational_note를 비워버리는 문제가 있었다.
    """
    educational_note_example = (
        {"included": True, "text": "자외선은 피부 노화를 유발할 수 있어요."}
        if needs_education
        else {"included": False, "text": ""}
    )
    structure_example = {stage.key: "..." for stage in fmt.stages}
    structure_example[fmt.cta_stage] = "... 제품정보는 본문에 있어요, 확인해보세요."
    return {
        "structure": structure_example,
        "educational_note": educational_note_example,
        "tone": fmt.label,
        "scenes": [
            {
                "seq": 1,
                "stage": fmt.stages[0].key,
                "situation": "situation_hook",
                "narration": "...",
                "caption": "...",
                "visual": "긴장감: 기기를 조심스럽게 만지며 불안해하는 손 클로즈업",
                "image_index": 0,
                "duration_sec": 5,
            },
            {
                "seq": 2,
                "stage": fmt.cta_stage,
                "situation": "situation_cta",
                "narration": "...",
                "caption": "...",
                "visual": "만족감: 완성된 결과물을 보며 흐뭇해하는 컷",
                "image_index": 1,
                "duration_sec": 5,
            },
        ],
        "disclosure": PARTNERS_DISCLOSURE,
        "estimated_duration_sec": 45,
        "youtube": {
            "title": "...",
            "description": f"... 구매 링크는 본문 더보기에서 확인해 주세요.\n\n{PARTNERS_DISCLOSURE}",
            "tags": ["..."],
        },
    }


def build_system_prompt(tone: str, needs_education: bool) -> str:
    if tone not in all_known_tones():
        raise ValueError(f"tone은 {all_known_tones()} 중 하나여야 합니다: {tone}")
    fmt = get_format(tone)
    stage_keys = fmt.stage_keys

    education_block = (
        EDUCATIONAL_NOTE_INSTRUCTION_TEMPLATE.format(stage_label=fmt.educational_note_after_stage)
        if needs_education
        else NO_EDUCATIONAL_NOTE_INSTRUCTION
    )
    schema_example = _build_output_schema_example(needs_education, fmt)

    return f"""너는 쿠팡 파트너스 쇼츠 대본을 쓰는 AI 직원이다.

{fmt.structure_instruction}

톤 지시: {fmt.tone_instruction}

후기 분석 결과의 suggested_hook_angle과 emotional_keywords를 {stage_keys[0]} 문장에 직접 반영하라.
예: 분석 결과에 "갱년기", "새벽에 깬다"가 반복되면 훅은 "요즘 새벽마다 깨시나요?"처럼 만든다.

{education_block}

{PRICE_INSTRUCTION}

{DISCLOSURE_PLACEMENT_INSTRUCTION}

제약(반드시 지켜야 하며, 어기면 시스템이 자동으로 반려한다):
- scenes 배열의 원소 개수는 반드시 3개 이상 8개 이하다. 9개 이상은 절대 금지. {len(stage_keys)}단계
  구조를 scene 여러 개로 나누더라도 총합이 8을 넘지 않게 통합하라 (예: 두 단계를 한 scene에 같이
  담아도 된다).
- scenes[].stage에는 그 씬이 구조(structure)의 어느 단계에 해당하는지 {"/".join(stage_keys)} 중
  정확히 하나를 넣는다. 한 단계를 씬 여러 개로 나눠 썼다면 그 씬들 모두 같은 stage 값을 쓴다.
  scenes는 stage 순서대로({" -> ".join(stage_keys)}) 정렬한다.
- scenes[].situation에는 stage와 별개로, 이 형식이 무엇이든 항상 다음 5개 값 중 정확히 하나를
  넣는다 — 이미지 생성 단계가 이 값으로 씬마다 다른 화면 스타일(정보 전달형/시연형/마무리형 등)을
  고른다:
{chr(10).join(f"  - {key}: {desc}" for key, desc in SITUATION_DESCRIPTIONS.items())}
  situation은 stage 이름과 문자 그대로 일치할 필요 없다 — 그 씬의 실제 내용(공감/문제 제기인지,
  근거·설계 설명인지, 사용 시연인지, 결과 정리인지, 구매 유도인지)을 보고 판단한다.
- narration을 모두 합친 영상 길이(estimated_duration_sec)는 30~45초.
- scenes[].duration_sec은 그 씬 narration을 한국어 TTS로 자연스럽게 읽는 데 걸리는 시간(초)을
  추정해서 쓴다(대략 초당 5~6글자 기준 — 실제 TTS 실측 결과를 반영한 수치다). 실제 렌더링에서
  이 값이 그 씬의 길이로 쓰이므로, narration 글자 수와 동떨어진 숫자(너무 짧거나 너무 긴 값)를
  넣으면 음성이 부자연스럽게 빨라지거나 느려진다. 특히 "제품정보는 본문에 있어요, 확인해보세요"
  처럼 짧은 CTA 문장에 실제보다 넉넉한 시간을 잡지 않도록 주의한다 — 문장이 짧으면 duration_sec도
  짧게 잡아야 한다.
- scenes[].visual에는 그 씬에 어울리는 화면 연출을 한 문장으로 짧게 적는다: "감정/분위기 태그: 구체적으로
  어떤 장면·구도·표정이 보이면 좋을지" 형태로 쓴다 (예: "답답함: 잘 안 붙는 부품을 만지며 인상 쓰는 손").
  이 텍스트는 실제 촬영 지시가 아니라, 후보 사진 중 가장 어울리는 컷을 고르는 데 참고용으로 쓰인다.
- scenes[].image_index는 0 이상 (user 메시지의 image_count - 1) 이하의 정수만 쓴다. 실제로 어떤 사진이
  쓰일지는 나중에 visual 문구를 보고 별도로 다시 고르므로, 여기서는 이미지 여러 장을 씬마다 최대한
  고르게 순환시켜 임시로 채운다(예: image_count=4면 0,1,2,3,0,1,2,3), image_count가 1이면 전부 0으로 둔다.
- narration에는 리뷰 원문 문장을 그대로 옮기지 않는다 (재구성된 표현만 사용).
- {fmt.cta_stage} 단계 narration에는 실제 상품명(user 메시지의 product_name 값)을 쓰지 않는다.
  "이 제품은"/"이 제품이" 등으로 지칭한다.
- disclosure 필드에는 반드시 다음 문구를 정확히 그대로 넣는다: "{PARTNERS_DISCLOSURE}"
- tone 필드에는 "{tone}"을 그대로 넣는다.
- educational_note 필드는 위 지시를 정확히 따른다. 아래 예시의 included/text 값을 그대로 참고하라
  (예시가 아니라 지시 내용이 기준이다 — 이 상품의 needs_education은 {str(needs_education).lower()}이다).

다른 설명 없이 아래와 같은 형태의 JSON 객체 하나만 출력한다 (scenes는 예시일 뿐 실제로는 3~8개를 채운다):
{json.dumps(schema_example, ensure_ascii=False, indent=2)}"""


def build_user_prompt(
    analysis_json: dict,
    product: dict,
    target_persona: str = DEFAULT_TARGET_PERSONA,
) -> str:
    payload = {
        "product_name": product.get("product_name"),
        "category": product.get("category"),
        "deeplink": product.get("deeplink"),
        "target_persona": target_persona,
        "image_count": len(product.get("image_urls") or []) or 1,
        "analysis": analysis_json,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
