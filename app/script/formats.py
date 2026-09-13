"""3호 직원 — 대본 "형식(format)" 레지스트리.

tone은 더 이상 문장 하나가 아니라 구조(stage 시퀀스) + 톤 지시문을 함께 정의하는
"형식"의 키다. 레지스트리 키는 scripts.tone 컬럼에 그대로 저장되는 문자열이며,
구버전(레거시) 7종도 영구히 키로 남아 있어야 기존 행이 계속 렌더링/편집된다 —
`legacy=True`인 항목은 신규 생성 선택지(`active_tone_choices`)에서만 제외되고,
조회(`get_format`)/검증(`all_known_tones`)에서는 계속 유효하다.

docs/03_interfaces.md 4번 스키마 + docs/00_project_overview.md "대본 설계" 절 기준.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 형식(tone) 7종이 각자 다른 stage 키 체계를 쓰기 때문에(아래 SCRIPT_FORMATS 참고),
# stage 값 자체는 이미지 생성 스타일 프리셋의 매핑 키로 쓸 수 없다 — 형식이 늘어날 때마다
# 매핑표도 같이 늘려야 하는 문제가 생긴다. 그래서 stage와 별개로, 형식과 무관하게
# 항상 같은 5개 값 중 하나를 쓰는 situation 태그를 scenes[]에 둔다(사용자 피드백,
# 2026-08-18) — app/media/image_generator.py의 SITUATION_STYLE_PRESETS가 이 값으로
# 씬마다 다른 이미지 스타일을 얹는다.
SITUATION_CHOICES: tuple[str, ...] = (
    "situation_hook",
    "situation_analysis",
    "situation_usage",
    "situation_conclusion",
    "situation_cta",
)

SITUATION_DESCRIPTIONS: dict[str, str] = {
    "situation_hook": "문제 제기/공감/해프닝 — 아직 해결책(상품)이 나오기 전, 감정을 건드리는 장면",
    "situation_analysis": "정보·근거 전달, 데이터/설계 포인트 강조 — 사실설명이나 비교·체크리스트처럼 설명 위주인 장면",
    "situation_usage": "실제 사용/시연 — 상품을 직접 쓰는 모습을 보여주는 장면",
    "situation_conclusion": "최종 결론/추천 대상 — 결과와 만족감을 정리하는 장면",
    "situation_cta": "구매 링크로 유도하는 마무리 장면",
}


@dataclass(frozen=True)
class StageSpec:
    key: str  # structure 딕셔너리 키 / scenes[].stage 값
    label: str  # 한글 라벨 — 대본작성 탭 카드 제목 등에 쓰인다
    seconds: int  # 권장 초 배분(가이드용 — estimated_duration_sec을 강제하진 않는다)


@dataclass(frozen=True)
class ScriptFormat:
    label: str
    legacy: bool  # True면 신규 생성 선택지에서 제외(하위호환 조회 전용)
    stages: tuple[StageSpec, ...]
    pre_reveal_stages: frozenset[str]  # 아직 실사 상품 사진/상시 CTA 배너를 보여주면 안 되는 구간
    cta_stage: str  # structure의 어느 필드가 "상품 등장 + CTA" 필드인지
    educational_note_after_stage: str  # educational_note를 이 stage 직후에 삽입하라고 안내
    structure_instruction: str  # 이 형식만의 구조 설명 프롬프트 블록
    tone_instruction: str  # 이 형식만의 문체 지시 한 줄

    @property
    def stage_keys(self) -> list[str]:
        return [s.key for s in self.stages]

    def label_for(self, key: str) -> str:
        return next((s.label for s in self.stages if s.key == key), key)


# --- 표준(구) 6단계 스켈레톤 — 레거시 7종 + 신규 3종(심리자극형/살림·생활 직설형/
#     인스타 생활설득형)이 공유한다. ---
STANDARD_STAGES: tuple[StageSpec, ...] = (
    StageSpec("empathy", "공감", 3),
    StageSpec("emotion", "감정", 4),
    StageSpec("problem", "문제제기", 6),
    StageSpec("solution", "해결", 8),
    StageSpec("result", "결과", 5),
    StageSpec("product", "상품", 6),
)
STANDARD_PRE_REVEAL: frozenset[str] = frozenset({"empathy", "emotion", "problem"})
STANDARD_STRUCTURE_INSTRUCTION = """구조는 정확히 6단계로 고정한다: 공감 -> 감정 -> 문제제기 -> 해결 -> 결과 -> 상품.
- empathy(공감, 권장 약 3초): 첫머리, 타겟이 "어 이거 내 얘기네" 하는 공감 문장
- emotion(감정, 권장 약 4초): 공감을 구체적 감정(답답함/불안/설렘 등)으로 증폭시키는 문장
- problem(문제제기, 권장 약 6초): 그 감정의 원인이 되는 문제를 명확히 짚는 문장
- solution(해결, 권장 약 8초): 문제를 해결하는 방향성 제시 (아직 상품명 등장 전)
- result(결과, 권장 약 5초): 그 방향대로 했을 때 실제로 어떤 효과·변화가 있었는지 구체적으로
  보여주는 문장 — 리뷰의 긍정 후기/재구매 이유를 재구성해서 쓴다 (원문 그대로 옮기지 않는다)
- product(상품, 권장 약 6초): 상품 등장 + CTA까지 포함하는 마무리 문장들 (가격은 언급하지 않는다).
  실제 상품명은 절대 쓰지 않고 "이 제품은", "이 제품이" 처럼 지칭한다.
CTA는 별도 단계가 아니라 product 단계 마지막 문장에 자연스럽게 포함한다."""


def _standard_format(label: str, legacy: bool, tone_instruction: str) -> ScriptFormat:
    return ScriptFormat(
        label=label,
        legacy=legacy,
        stages=STANDARD_STAGES,
        pre_reveal_stages=STANDARD_PRE_REVEAL,
        cta_stage="product",
        educational_note_after_stage="problem",
        structure_instruction=STANDARD_STRUCTURE_INSTRUCTION,
        tone_instruction=tone_instruction,
    )


# --- 기획천재발견형: 제품 설계의 영리함에 감탄하는 구조 ---
_GIPHOK_STAGES = (
    StageSpec("hook", "후킹", 3),
    StageSpec("discovery", "발견", 5),
    StageSpec("design_insight", "설계 인사이트", 10),
    StageSpec("more_details", "디테일", 6),
    StageSpec("daily_use", "일상 적용", 6),
    StageSpec("product_cta", "상품/CTA", 6),
)
_GIPHOK_STRUCTURE_INSTRUCTION = """구조는 정확히 6단계로 고정한다: 후킹 -> 발견 -> 설계 인사이트 -> 디테일 -> 일상 적용 -> 상품/CTA.
- hook(후킹, 권장 약 3초): 사소하지만 다들 겪는 불편한 순간을 툭 던진다
- discovery(발견, 권장 약 5초): 그 불편을 해결하는 물건을 우연히 발견한 듯 소개한다 (아직 상품명 등장 전)
- design_insight(설계 인사이트, 권장 약 10초): "이거 만든 사람 천재 아니야?" 싶은 구체적인 설계
  포인트 하나에 감탄한다 — 이 형식의 핵심 단계. 막연한 칭찬이 아니라 어떤 디테일이 왜 영리한지
  구체적으로 짚는다
- more_details(디테일, 권장 약 6초): 감탄할 만한 설계 포인트를 한두 개 더 짚어 "곳곳이 다 이런
  식"이라는 인상을 쌓는다
- daily_use(일상 적용, 권장 약 6초): 실제로 써봤을 때 일상이 얼마나 편해졌는지 구체적으로
  보여준다 — 리뷰의 긍정 후기/재구매 이유를 재구성해서 쓴다 (원문 그대로 옮기지 않는다)
- product_cta(상품/CTA, 권장 약 6초): 상품 등장 + CTA까지 포함하는 마무리 문장들 (가격은
  언급하지 않는다). 실제 상품명은 절대 쓰지 않고 "이 제품은", "이 제품이" 처럼 지칭한다.
CTA는 별도 단계가 아니라 product_cta 단계 마지막 문장에 자연스럽게 포함한다."""

# --- 인테리어전문가형: 배치/동선 전문가의 전후비교 구조 ---
_INTERIOR_STAGES = (
    StageSpec("hook", "후킹", 3),
    StageSpec("before", "배치 전", 6),
    StageSpec("expert_tip", "전문가 팁", 8),
    StageSpec("placement", "배치 적용", 8),
    StageSpec("after", "배치 후", 6),
    StageSpec("product_cta", "상품/CTA", 6),
)
_INTERIOR_STRUCTURE_INSTRUCTION = """구조는 정확히 6단계로 고정한다: 후킹 -> 배치 전 -> 전문가 팁 -> 배치 적용 -> 배치 후 -> 상품/CTA.
- hook(후킹, 권장 약 3초): 좁고 답답하거나 동선이 꼬인 공간의 흔한 문제 상황을 툭 던진다
- before(배치 전, 권장 약 6초): 그 공간이 왜 불편한지(배치/동선 문제) 구체적으로 짚는다
- expert_tip(전문가 팁, 권장 약 8초): 집 정리·동선 전문가가 알려주는 원칙 하나를 제시한다
  (아직 상품명 등장 전) — 이 형식의 핵심 단계
- placement(배치 적용, 권장 약 8초): 그 원칙을 실제로 적용하는 과정을 보여준다. 상품이 그 원칙을
  실현하는 도구로 자연스럽게 등장한다
- after(배치 후, 권장 약 6초): 배치를 바꾼 뒤 얼마나 넓어지고 편해졌는지 구체적으로 보여준다 —
  리뷰의 긍정 후기/재구매 이유를 재구성해서 쓴다 (원문 그대로 옮기지 않는다)
- product_cta(상품/CTA, 권장 약 6초): 상품 등장 + CTA까지 포함하는 마무리 문장들 (가격은
  언급하지 않는다). 실제 상품명은 절대 쓰지 않고 "이 제품은", "이 제품이" 처럼 지칭한다.
CTA는 별도 단계가 아니라 product_cta 단계 마지막 문장에 자연스럽게 포함한다."""

# --- 글로벌 인사이트형: 비즈니스 트렌드/인사이트로 프레이밍하는 구조 ---
_GLOBAL_STAGES = (
    StageSpec("insight_hook", "인사이트 후킹", 4),
    StageSpec("trend_context", "트렌드 배경", 8),
    StageSpec("logic_application", "개인 적용", 8),
    StageSpec("product_solution", "상품 연결", 8),
    StageSpec("result", "결과", 6),
    StageSpec("cta", "마무리 CTA", 5),
)
_GLOBAL_STRUCTURE_INSTRUCTION = """구조는 정확히 6단계로 고정한다: 인사이트 후킹 -> 트렌드 배경 -> 개인 적용 -> 상품 연결 -> 결과 -> 마무리 CTA.
- insight_hook(인사이트 후킹, 권장 약 4초): "잘나가는 사람/기업들은 이렇게 한다"는 식의 인사이트
  한 줄로 시작한다
- trend_context(트렌드 배경, 권장 약 8초): 그 인사이트/트렌드가 왜 지금 중요한지 배경을 짚는다
  (아직 상품명 등장 전)
- logic_application(개인 적용, 권장 약 8초): 그 비즈니스 로직을 개인의 일상/소비 결정에 적용하면
  어떤 의미인지 연결한다 — 이 형식의 핵심 단계
- product_solution(상품 연결, 권장 약 8초): 그 논리적 결론의 실천으로 상품을 자연스럽게 연결한다
- result(결과, 권장 약 6초): 그렇게 선택했을 때 실제로 어떤 효과·변화가 있었는지 구체적으로
  보여준다 — 리뷰의 긍정 후기/재구매 이유를 재구성해서 쓴다 (원문 그대로 옮기지 않는다)
- cta(마무리 CTA, 권장 약 5초): 상품 등장 + CTA까지 포함하는 마무리 문장들 (가격은 언급하지
  않는다). 실제 상품명은 절대 쓰지 않고 "이 제품은", "이 제품이" 처럼 지칭한다.
CTA는 별도 단계가 아니라 cta 단계 마지막 문장에 자연스럽게 포함한다."""

# --- 썸쇼츠형: 스토리텔링으로 상품을 연결하는 구조 ---
_SSUM_STAGES = (
    StageSpec("story_setup", "도입", 5),
    StageSpec("story_turn", "전개", 8),
    StageSpec("story_twist", "반전", 8),
    StageSpec("story_resolution", "마무리 스토리", 8),
    StageSpec("product_cta", "상품/CTA", 6),
)
_SSUM_STRUCTURE_INSTRUCTION = """구조는 정확히 5단계로 고정한다: 도입 -> 전개 -> 반전 -> 마무리 스토리 -> 상품/CTA.
- story_setup(도입, 권장 약 5초): 짧은 인물 중심 에피소드(예: 썸 타는 두 사람, 사소한 일상
  상황)를 던진다 — 상품/광고 이야기가 아니라 "이야기"로 시작한다 (아직 상품명 등장 전)
- story_turn(전개, 권장 약 8초): 그 이야기를 조금 더 구체적으로 전개한다 (아직 상품명 등장 전)
- story_twist(반전, 권장 약 8초): 이야기의 전환점에 상품이 자연스럽게 등장해 상황을 풀어준다 —
  이 형식의 핵심 단계
- story_resolution(마무리 스토리, 권장 약 8초): 상품 덕분에 달라진 결말을 보여준다 — 리뷰의
  긍정 후기/재구매 이유를 재구성해서 쓴다 (원문 그대로 옮기지 않는다)
- product_cta(상품/CTA, 권장 약 6초): 상품 등장 + CTA까지 포함하는 마무리 문장들 (가격은
  언급하지 않는다). 실제 상품명은 절대 쓰지 않고 "이 제품은", "이 제품이" 처럼 지칭한다.
CTA는 별도 단계가 아니라 product_cta 단계 마지막 문장에 자연스럽게 포함한다."""


SCRIPT_FORMATS: dict[str, ScriptFormat] = {
    # --- 레거시 7종 (v2.4 이전 생성분 하위호환 전용 — 신규 생성 UI/API엔 노출 안 함) ---
    "불편해결": _standard_format(
        "불편해결", True, "일상의 불편함을 짚고, 상품이 그 불편을 없애준다는 실용적 해결 톤으로 쓴다."
    ),
    "우월감": _standard_format(
        "우월감", True, "이 상품을 쓰면 남들보다 한발 앞선 선택을 한다는 느낌을 주는 톤으로 쓴다."
    ),
    "보상": _standard_format("보상", True, "고생한 나에게 주는 선물/보상이라는 톤으로 쓴다."),
    "생활팁": _standard_format("생활팁", True, "생활 노하우를 공유하는 친근한 정보성 톤으로 쓴다."),
    "사실형": _standard_format("사실형", True, "과장 없이 담백하게 사실과 근거 위주로 쓴다."),
    "생활형": _standard_format("생활형", True, "일상 대화하듯 편안하고 자연스러운 톤으로 쓴다."),
    "실리적": _standard_format(
        "실리적", True, "가격 숫자 대신 '가성비'/'효율' 같은 개념으로 실속을 강조하는 톤으로 쓴다."
    ),
    # --- 신규 7종 중 표준 6단계를 재사용하는 3종 ---
    "심리자극형": _standard_format(
        "심리자극형",
        False,
        "이 상품을 쓰고 나서 달라진 나(더 당당해진, 남들보다 한발 앞선 선택을 한 느낌)를 강조하면서, "
        "동시에 고생한 나에게 주는 보상이라는 심리를 함께 자극하는 톤으로 쓴다 — '이걸 쓰는 나는 이미 "
        "다른 사람과 다르다'는 우월감과 '이 정도는 나를 위해 써도 된다'는 자기합리화를 함께 담는다.",
    ),
    "살림/생활 직설형": _standard_format(
        "살림/생활 직설형",
        False,
        "군더더기 설명 없이 핵심만 툭툭 던지는 직설적인 말투로 실생활 살림 노하우를 알려주듯 쓴다 — "
        "'~하세요', '~쓰세요' 같은 명령형/단정형 문장 위주로 담백하고 효율적으로 전달한다.",
    ),
    "인스타 생활설득형": _standard_format(
        "인스타 생활설득형",
        False,
        "인스타그램 라이프스타일 인플루언서가 감성적인 일상 컷과 함께 자연스럽게 소개하듯, 동경할 "
        "만한 분위기(감성/무드)를 은은하게 강조하며 설득하는 톤으로 쓴다 — 직접적인 광고보다 '내 "
        "일상에 자연스럽게 녹아든 아이템'이라는 느낌을 준다.",
    ),
    # --- 신규 7종 중 전용 구조가 필요한 4종 ---
    "기획천재발견형": ScriptFormat(
        label="기획천재발견형",
        legacy=False,
        stages=_GIPHOK_STAGES,
        pre_reveal_stages=frozenset({"hook"}),
        cta_stage="product_cta",
        educational_note_after_stage="hook",
        structure_instruction=_GIPHOK_STRUCTURE_INSTRUCTION,
        tone_instruction=(
            "제품을 만든 사람의 설계 감각에 진심으로 감탄하는 톤으로 쓴다 — '이거 만든 사람 천재 "
            "아니야?' 싶은 구체적인 디테일을 발견해가는 리액션처럼, 호들갑스럽지 않게 담담한 놀라움을 "
            "담아 쓴다."
        ),
    ),
    "인테리어전문가형": ScriptFormat(
        label="인테리어전문가형",
        legacy=False,
        stages=_INTERIOR_STAGES,
        pre_reveal_stages=frozenset({"hook", "before", "expert_tip"}),
        cta_stage="product_cta",
        educational_note_after_stage="before",
        structure_instruction=_INTERIOR_STRUCTURE_INSTRUCTION,
        tone_instruction=(
            "집 정리·동선 전문가가 상담하듯 단정적이고 신뢰감 있는 어조로 배치/동선 원칙을 설명하고, "
            "상품을 그 원칙의 실전 예시로 자연스럽게 소개하는 톤으로 쓴다."
        ),
    ),
    "글로벌 인사이트형": ScriptFormat(
        label="글로벌 인사이트형",
        legacy=False,
        stages=_GLOBAL_STAGES,
        pre_reveal_stages=frozenset({"insight_hook", "trend_context", "logic_application"}),
        cta_stage="cta",
        educational_note_after_stage="trend_context",
        structure_instruction=_GLOBAL_STRUCTURE_INSTRUCTION,
        tone_instruction=(
            "'전세계 잘나가는 사람/기업들은 이렇게 한다'는 비즈니스 인사이트 유튜버 프레이밍으로, 상품 "
            "구매를 하나의 합리적 의사결정처럼 설득하는 톤으로 쓴다."
        ),
    ),
    "썸쇼츠형": ScriptFormat(
        label="썸쇼츠형",
        legacy=False,
        stages=_SSUM_STAGES,
        pre_reveal_stages=frozenset({"story_setup", "story_turn"}),
        cta_stage="product_cta",
        educational_note_after_stage="story_setup",
        structure_instruction=_SSUM_STRUCTURE_INSTRUCTION,
        tone_instruction=(
            "직접적인 상품 설명 대신 짧은 인물 중심 스토리(예: 썸 타는 두 사람, 사소한 일상 에피소드)를 "
            "먼저 들려주고, 이야기의 전환점에 상품이 자연스럽게 등장해 이야기를 풀어주는 구조로 쓴다 — "
            "광고 멘트보다 '이야기'가 먼저다."
        ),
    ),
}

# get_format()이 미등록/손상된 tone 값을 만났을 때 쓰는 안전한 기본값 — 렌더링/편집 경로가
# 이것 때문에 죽으면 안 된다.
_FALLBACK_TONE = "생활팁"


def get_format(tone: str | None) -> ScriptFormat:
    """알려지지 않은 tone 값이 와도 예외를 던지지 않고 표준 6단계 형식으로 안전하게 폴백한다."""
    fmt = SCRIPT_FORMATS.get(tone or "")
    if fmt is None:
        logger.warning("알 수 없는 tone '%s' — 표준 6단계 형식으로 폴백합니다.", tone)
        fmt = SCRIPT_FORMATS[_FALLBACK_TONE]
    return fmt


def active_tone_choices() -> list[str]:
    """신규 대본 생성 시 UI 드롭다운/API에 노출할 목록 — 신규 7종만."""
    return [key for key, fmt in SCRIPT_FORMATS.items() if not fmt.legacy]


def all_known_tones() -> list[str]:
    """'실존하는 tone인가'를 판정할 때 쓰는 전체 목록(레거시 7 + 신규 7)."""
    return list(SCRIPT_FORMATS.keys())
