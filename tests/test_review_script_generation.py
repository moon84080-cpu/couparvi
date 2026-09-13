import json

import pytest

from app.review.analyzer import ReviewAnalysisError, analyze_reviews
from app.script.generator import generate_script


class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeContentBlock(text)]


class _FakeMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        text = self._responses.pop(0)
        return _FakeMessage(text)


class _FakeClient:
    def __init__(self, responses: list[str]):
        self.messages = _FakeMessages(responses)


VALID_ANALYSIS = {
    "positives": ["부드러운 원단"],
    "complaints": ["얇게 느껴질 수 있음"],
    "surprises": ["가성비 대비 만족도가 높다"],
    "repurchase_reasons": ["대용량이라 쟁여두기 좋음"],
    "emotional_keywords": ["안심", "든든함"],
    "target_segments": ["생필품 재구매층"],
    "suggested_hook_angle": "믿고 쓰는 브랜드로 갈아탄 이유",
}


def test_analyze_reviews_parses_valid_json():
    client = _FakeClient([json.dumps(VALID_ANALYSIS, ensure_ascii=False)])
    result = analyze_reviews("아주 좋아요 부드럽고 편해요", client=client)
    assert result == VALID_ANALYSIS
    assert client.messages.calls == 1


def test_analyze_reviews_retries_once_on_bad_json_then_succeeds():
    client = _FakeClient(["이건 JSON이 아님", json.dumps(VALID_ANALYSIS, ensure_ascii=False)])
    result = analyze_reviews("리뷰", client=client)
    assert result == VALID_ANALYSIS
    assert client.messages.calls == 2


def test_analyze_reviews_raises_after_two_failures():
    client = _FakeClient(["이건 JSON이 아님", "여전히 JSON이 아님"])
    with pytest.raises(ReviewAnalysisError):
        analyze_reviews("리뷰", client=client)
    assert client.messages.calls == 2


def test_analyze_reviews_rejects_missing_fields():
    incomplete = {"positives": ["좋아요"]}
    client = _FakeClient([json.dumps(incomplete), json.dumps(incomplete)])
    with pytest.raises(ReviewAnalysisError):
        analyze_reviews("리뷰", client=client)


VALID_SCRIPT = {
    "structure": {
        "empathy": "e",
        "emotion": "e2",
        "problem": "p",
        "solution": "s",
        "product": "prod",
    },
    "educational_note": {"included": False, "text": ""},
    "tone": "생활팁",
    "scenes": [
        {
            # 나레이션 길이(27자)와 duration_sec(5초)이 초당 5.5자 기준과 정확히 맞아떨어지게
            # 골랐다 — generate_script()의 duration_sec 자동 재설정(app/script/generator.py의
            # _normalize_scene_durations, 사용자 피드백 2026-08-19)이 값을 바꾸지 않아야
            # 이 테스트가 순수하게 "JSON 파싱 결과 그대로 반환"만 검증할 수 있다.
            "seq": 1,
            "narration": "이 제품은 정말 만족스러운 선택이었어요 강추합니다",
            "caption": "c",
            "image_index": 0,
            "duration_sec": 5,
        }
    ],
    "disclosure": "d",
    "estimated_duration_sec": 40,
    "youtube": {"title": "t", "description": "d", "tags": []},
}


def test_generate_script_parses_valid_json():
    client = _FakeClient([json.dumps(VALID_SCRIPT, ensure_ascii=False)])
    result = generate_script(
        analysis_json=VALID_ANALYSIS,
        product={"product_name": "테스트 상품", "price": 10000, "category": "생활용품"},
        tone="생활팁",
        needs_education=False,
        client=client,
    )
    assert result == VALID_SCRIPT


def test_generate_script_normalizes_mismatched_scene_duration():
    # LLM이 "초당 5~6글자" 가이드를 못 지켜 나레이션(34자)에 비해 너무 짧은 duration_sec(3초)을
    # 낸 경우 — 그대로 두면 렌더링에서 속도 clamp로 강제 조정되며 결과가 크게 어긋난다
    # (사용자 피드백, 2026-08-19, 라이브박스 2세대 대본 실측). generate_script()가 대본
    # (나레이션) 기준으로 자동 재설정해야 한다.
    mismatched_script = json.loads(json.dumps(VALID_SCRIPT))
    mismatched_script["scenes"] = [
        {
            "seq": 1,
            "narration": "야외나 차 안에서 맛있는 에스프레소 진짜 절실할 때 많으셨죠",
            "caption": "c",
            "image_index": 0,
            "duration_sec": 3,
        }
    ]
    client = _FakeClient([json.dumps(mismatched_script, ensure_ascii=False)])
    result = generate_script(
        analysis_json=VALID_ANALYSIS,
        product={"product_name": "테스트 상품", "price": 10000, "category": "가전디지털"},
        tone="생활팁",
        needs_education=False,
        client=client,
    )
    assert result["scenes"][0]["duration_sec"] != 3
    assert result["scenes"][0]["duration_sec"] > 4
