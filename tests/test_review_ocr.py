import io

import pytest
from PIL import Image, ImageDraw

from app.review import ocr
from app.review.ocr import OCR_MAX_UPSCALE, OCR_MIN_DIMENSION_PX, ReviewOcrError, _preprocess_for_ocr


def _png_bytes(size: tuple[int, int]) -> bytes:
    img = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _png_bytes_with_edges(size: tuple[int, int]) -> bytes:
    # 완전 단색 이미지는 선명화(unsharp mask)가 적용돼도 바이트가 안 바뀐다(엣지가
    # 없어서) — 순수 흑백(0/255) 엣지도 마찬가지다(보정값이 0~255 범위를 벗어나
    # 클리핑되면서 결국 원래 값으로 되돌아간다). 클리핑 없이 실제로 값이 바뀌는 걸
    # 보려면 중간 톤(회색) 사각형처럼 여유가 있는 대비가 필요하다.
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4], fill=(180, 180, 180)
    )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_small_image_gets_upscaled_to_min_dimension():
    # 짧은 변(300)에 필요한 배율(800/300≈2.67)이 OCR_MAX_UPSCALE(4) 이내라 목표 크기까지 커진다.
    original = _png_bytes((900, 300))
    result_bytes, media_type = _preprocess_for_ocr(original, "image/png")

    result = Image.open(io.BytesIO(result_bytes))
    assert media_type == "image/png"
    assert min(result.width, result.height) >= OCR_MIN_DIMENSION_PX - 1  # 반올림 오차 허용
    assert result.width > 900


def test_large_image_is_not_upscaled_but_is_sharpened():
    # 이미 충분히 큰 이미지는 크기를 키우지 않지만, 선명화는 크기와 무관하게 항상
    # 적용한다(사용자 피드백: 확대해도 여전히 인식률이 낮음 — 흐림이 원인일 수 있음).
    original = _png_bytes_with_edges((1000, 1000))
    result_bytes, media_type = _preprocess_for_ocr(original, "image/jpeg")

    result = Image.open(io.BytesIO(result_bytes))
    assert media_type == "image/png"  # 항상 재인코딩되므로 PNG로 통일된다
    assert result.size == (1000, 1000)  # 크기는 그대로
    assert result_bytes != original  # 선명화가 적용돼 바이트는 달라짐


def test_tiny_image_upscale_is_capped():
    original = _png_bytes((20, 20))
    result_bytes, _ = _preprocess_for_ocr(original, "image/png")
    result = Image.open(io.BytesIO(result_bytes))
    assert result.width == 20 * OCR_MAX_UPSCALE


def test_unparseable_bytes_pass_through_without_crashing():
    garbage = b"not an image"
    result_bytes, media_type = _preprocess_for_ocr(garbage, "image/png")
    assert result_bytes == garbage
    assert media_type == "image/png"


# --- OCR_PROVIDER 전환(TTS_PROVIDER와 같은 패턴, 사용자 피드백 2026-08-18) ---


class _FakeOpenAIResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeOpenAIClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response


def _png_bytes_small(size=(10, 10)) -> bytes:
    img = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_extract_review_text_uses_openai_when_provider_is_openai(monkeypatch):
    monkeypatch.setattr(ocr, "OPENAI_API_KEY", "fake-openai-key")
    client = _FakeOpenAIClient(
        _FakeOpenAIResponse(200, {"choices": [{"message": {"content": "추출된 리뷰 텍스트"}}]})
    )

    result = ocr.extract_review_text(_png_bytes_small(), "image/png", client=client, provider="openai")

    assert result == "추출된 리뷰 텍스트"
    assert len(client.calls) == 1
    body = client.calls[0]["json"]
    assert body["model"] == ocr.OPENAI_MODEL
    # 이미지가 OpenAI의 image_url(data URI) 형식으로 들어가야 한다(Anthropic의
    # {"type":"image","source":{...}} 형식과 다르다).
    image_part = body["messages"][1]["content"][0]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_review_text_openai_requires_api_key(monkeypatch):
    monkeypatch.setattr(ocr, "OPENAI_API_KEY", "")
    with pytest.raises(ReviewOcrError, match="OPENAI_API_KEY"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", provider="openai")


def test_extract_review_text_openai_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(ocr, "OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setattr(ocr.time, "sleep", lambda *_a, **_k: None)
    client = _FakeOpenAIClient(_FakeOpenAIResponse(500, text="server error"))

    with pytest.raises(ReviewOcrError, match="OpenAI 요청 실패"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", client=client, provider="openai")


def test_extract_review_text_rejects_unknown_provider():
    with pytest.raises(ReviewOcrError, match="OCR_PROVIDER"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", provider="does-not-exist")


# --- OCR_PROVIDER=gemini (claude/openai보다 토큰 단가가 싸서 인식률 검증용으로 추가, 사용자 피드백 2026-08-18) ---


class _FakeGeminiClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, params=None, json=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return self._response


def test_extract_review_text_uses_gemini_when_provider_is_gemini(monkeypatch):
    monkeypatch.setattr(ocr, "GEMINI_API_KEY", "fake-gemini-key")
    response = _FakeOpenAIResponse(
        200,
        {"candidates": [{"content": {"parts": [{"text": "추출된 리뷰 텍스트"}]}}]},
    )
    client = _FakeGeminiClient(response)

    result = ocr.extract_review_text(_png_bytes_small(), "image/png", client=client, provider="gemini")

    assert result == "추출된 리뷰 텍스트"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["params"] == {"key": "fake-gemini-key"}
    # inlineData(base64) 형식으로 들어가야 한다(OpenAI의 image_url data URI, Anthropic의
    # {"type":"image","source":{...}} 형식과 다르다).
    image_part = call["json"]["contents"][0]["parts"][0]
    assert "inlineData" in image_part
    assert image_part["inlineData"]["mimeType"] == "image/png"


def test_extract_review_text_gemini_requires_api_key(monkeypatch):
    monkeypatch.setattr(ocr, "GEMINI_API_KEY", "")
    with pytest.raises(ReviewOcrError, match="GEMINI_API_KEY"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", provider="gemini")


def test_extract_review_text_gemini_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(ocr, "GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setattr(ocr.time, "sleep", lambda *_a, **_k: None)
    client = _FakeGeminiClient(_FakeOpenAIResponse(500, text="server error"))

    with pytest.raises(ReviewOcrError, match="Gemini 요청 실패"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", client=client, provider="gemini")


def test_extract_review_text_gemini_raises_when_no_candidates(monkeypatch):
    monkeypatch.setattr(ocr, "GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setattr(ocr.time, "sleep", lambda *_a, **_k: None)
    client = _FakeGeminiClient(_FakeOpenAIResponse(200, {"candidates": []}))

    with pytest.raises(ReviewOcrError, match="candidates"):
        ocr.extract_review_text(_png_bytes_small(), "image/png", client=client, provider="gemini")
