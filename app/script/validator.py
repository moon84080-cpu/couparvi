"""3호 직원 산출물 검증.

docs/03_interfaces.md 4번 스키마 + docs/phase2_checklist.md 기준.
스키마 검증, 원문 유출 검사, disclosure 일치, educational_note 규칙을 확인한다.
"""

from __future__ import annotations

import re

from app.config import EDUCATION_FORBIDDEN_KEYWORDS, PARTNERS_DISCLOSURE
from app.script.formats import SITUATION_CHOICES, all_known_tones, get_format

MIN_SCENES = 3
MAX_SCENES = 8
MIN_DURATION_SEC = 30
MAX_DURATION_SEC = 45
MIN_VERBATIM_OVERLAP_WORDS = 8

# v2.2: 가격은 어디에도 숫자로 표기하지 않는다 (사용자 피드백) — "18,900원", "2만원대" 등을 잡는다.
PRICE_PATTERN = re.compile(r"\d[\d,]*\s*만?\s*원")

# CTA는 "본문/더보기/설명란"에서 확인하도록 유도하는 문구를 포함해야 한다.
CTA_REDIRECT_HINTS = ["본문", "더보기", "설명란"]


class ScriptValidationError(RuntimeError):
    """반려 사유 목록을 담는 예외. args[0]는 사유 문자열 리스트."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _tokenize(text: str) -> list[str]:
    return text.split()


def has_verbatim_overlap(
    narration: str, reviews_raw: str, min_words: int = MIN_VERBATIM_OVERLAP_WORDS
) -> bool:
    """narration이 reviews_raw와 min_words개 이상 연속으로 일치하는 구간이 있는지 확인한다."""
    narration_tokens = _tokenize(narration)
    reviews_tokens = _tokenize(reviews_raw)
    if len(narration_tokens) < min_words or len(reviews_tokens) < min_words:
        return False
    for i in range(len(narration_tokens) - min_words + 1):
        window = narration_tokens[i : i + min_words]
        for j in range(len(reviews_tokens) - min_words + 1):
            if reviews_tokens[j : j + min_words] == window:
                return True
    return False


def validate_structure(script_json: dict) -> list[str]:
    errors = []
    structure = script_json.get("structure") or {}
    fmt = get_format(script_json.get("tone"))
    for field in fmt.stage_keys:
        if not structure.get(field):
            errors.append(f"structure.{field}가 비어있습니다.")
    return errors


def validate_tone(script_json: dict) -> list[str]:
    tone = script_json.get("tone")
    known_tones = all_known_tones()
    if tone not in known_tones:
        return [f"tone '{tone}'이 정의된 형식({known_tones})에 포함되지 않습니다."]
    return []


def validate_disclosure(script_json: dict) -> list[str]:
    if script_json.get("disclosure") != PARTNERS_DISCLOSURE:
        return ["disclosure가 PARTNERS_DISCLOSURE 상수와 일치하지 않습니다."]
    return []


def validate_scenes(script_json: dict) -> list[str]:
    errors = []
    scenes = script_json.get("scenes") or []
    if not (MIN_SCENES <= len(scenes) <= MAX_SCENES):
        errors.append(f"scenes 개수({len(scenes)})가 {MIN_SCENES}~{MAX_SCENES} 범위를 벗어났습니다.")

    duration = script_json.get("estimated_duration_sec")
    if not isinstance(duration, (int, float)) or not (MIN_DURATION_SEC <= duration <= MAX_DURATION_SEC):
        errors.append(
            f"estimated_duration_sec({duration})이 {MIN_DURATION_SEC}~{MAX_DURATION_SEC}초 범위를 벗어났습니다."
        )

    # 대본작성 탭에서 각 구조 카드마다 해당 자막을 같이 보여주려면 stage가 이 대본의 형식이
    # 정의한 stage 목록 중 하나로 정확히 채워져 있어야 한다 — 비어있거나 오타면 그 씬의
    # 자막이 어느 카드에도 안 붙어 조용히 누락된다.
    fmt = get_format(script_json.get("tone"))
    for scene in scenes:
        stage = scene.get("stage")
        if stage not in fmt.stage_keys:
            errors.append(f"scene {scene.get('seq', '?')}의 stage('{stage}')가 {fmt.stage_keys} 중 하나가 아닙니다.")
        # situation은 stage와 달리 형식(tone)에 상관없이 항상 같은 5개 값 중 하나다 — 이미지
        # 생성 스타일 프리셋(app/media/image_generator.py의 SITUATION_STYLE_PRESETS) 매칭에 쓰인다.
        situation = scene.get("situation")
        if situation not in SITUATION_CHOICES:
            errors.append(f"scene {scene.get('seq', '?')}의 situation('{situation}')이 {SITUATION_CHOICES} 중 하나가 아닙니다.")
    return errors


def validate_no_verbatim_leak(script_json: dict, reviews_raw: str) -> list[str]:
    errors = []
    for scene in script_json.get("scenes") or []:
        narration = scene.get("narration", "")
        if has_verbatim_overlap(narration, reviews_raw):
            seq = scene.get("seq", "?")
            errors.append(
                f"scene {seq}의 narration이 리뷰 원문과 {MIN_VERBATIM_OVERLAP_WORDS}단어 이상 연속 일치합니다."
            )
    return errors


def _narration_texts(script_json: dict) -> list[str]:
    structure = script_json.get("structure") or {}
    fmt = get_format(script_json.get("tone"))
    texts = [structure.get(field, "") or "" for field in fmt.stage_keys]
    texts += [scene.get("narration", "") or "" for scene in script_json.get("scenes") or []]
    return texts


def validate_no_price_mention(script_json: dict) -> list[str]:
    """narration과 youtube 텍스트 어디에도 가격을 숫자로 표기하지 않아야 한다 (v2.2)."""
    errors = []
    youtube = script_json.get("youtube") or {}
    fields_to_check = _narration_texts(script_json) + [
        youtube.get("title", "") or "",
        youtube.get("description", "") or "",
    ]
    for text in fields_to_check:
        match = PRICE_PATTERN.search(text)
        if match:
            errors.append(f"가격으로 보이는 표현 '{match.group()}'이 포함되어 있습니다 (가격은 표기하지 않습니다).")
    return errors


def validate_cta_redirects_to_description(script_json: dict) -> list[str]:
    """CTA 단계 narration이 '본문/더보기/설명란' 등으로 링크 확인을 유도해야 한다 (v2.2)."""
    structure = script_json.get("structure") or {}
    fmt = get_format(script_json.get("tone"))
    combined = structure.get(fmt.cta_stage, "") or ""
    if not any(hint in combined for hint in CTA_REDIRECT_HINTS):
        return [
            f"structure.{fmt.cta_stage}에 '본문/더보기/설명란' 등 링크 확인을 유도하는 CTA 문구가 없습니다."
        ]
    return []


def validate_disclosure_placement(script_json: dict) -> list[str]:
    """고지문구는 narration에는 없고, youtube.description 마지막 줄에만 있어야 한다 (v2.2)."""
    errors = []
    if any(PARTNERS_DISCLOSURE in text for text in _narration_texts(script_json)):
        errors.append("narration/structure에 고지문구(disclosure)가 포함되어 있습니다 (음성으로 낭독하지 않습니다).")

    description = ((script_json.get("youtube") or {}).get("description") or "").rstrip()
    if not description.endswith(PARTNERS_DISCLOSURE):
        errors.append("youtube.description이 고지문구로 끝나지 않습니다.")

    return errors


def validate_educational_note(script_json: dict, needs_education: bool) -> list[str]:
    errors = []
    note = script_json.get("educational_note") or {}
    included = note.get("included", False)
    text = note.get("text", "") or ""

    if needs_education:
        if not included or not text.strip():
            errors.append("needs_education=true인데 educational_note.included=false이거나 text가 비어있습니다.")
    else:
        if included:
            errors.append("needs_education=false인데 educational_note.included=true입니다.")

    for forbidden in EDUCATION_FORBIDDEN_KEYWORDS:
        if forbidden in text:
            errors.append(f"educational_note.text에 금지어 '{forbidden}'가 포함되어 있습니다.")

    return errors


def validate_script(script_json: dict, reviews_raw: str, needs_education: bool) -> None:
    """검증 실패 시 ScriptValidationError(errors 전체 목록)를 발생시킨다."""
    errors: list[str] = []
    errors += validate_structure(script_json)
    errors += validate_tone(script_json)
    errors += validate_disclosure(script_json)
    errors += validate_scenes(script_json)
    errors += validate_no_verbatim_leak(script_json, reviews_raw)
    errors += validate_educational_note(script_json, needs_education)
    errors += validate_no_price_mention(script_json)
    errors += validate_cta_redirects_to_description(script_json)
    errors += validate_disclosure_placement(script_json)

    if errors:
        raise ScriptValidationError(errors)
