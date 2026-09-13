import base64

import pytest

from app.media import image_generator
from app.media.image_generator import ImageGenerationError, generate_scene_image


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


class _FakeClient:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, params=None, json=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return self._responses.pop(0)


def _success_body(image_bytes: bytes) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }


def test_generate_scene_image_with_product_reference_returns_bytes(monkeypatch):
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nfakepngbytes"
    client = _FakeClient([_FakeResponse(200, _success_body(expected))])

    result, mime_type = generate_scene_image(
        "긴장감", "나레이션", "테스트 상품",
        product_reference=(b"reference", "image/jpeg"), client=client,
    )

    assert result == expected
    assert mime_type == "image/png"
    sent_body = client.calls[0]["json"]
    parts = sent_body["contents"][0]["parts"]
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert "테스트 상품" in parts[0]["text"]
    assert len(parts) == 2  # character_reference 없으면 참고 이미지는 상품 사진 1장뿐


def test_generate_scene_image_without_product_reference_omits_image_and_says_not_yet(monkeypatch):
    # 대본의 공감/문제 단계(pre-reveal) — 상품 참고 이미지 없이 순수 텍스트만으로 생성해야
    # 광고 대상 상품이 "문제 상황" 장면에 잘못 등장하지 않는다(사용자 피드백).
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nnoproductbytes"
    client = _FakeClient([_FakeResponse(200, _success_body(expected))])

    generate_scene_image("긴장감", "나레이션", "테스트 상품", client=client)

    parts = client.calls[0]["json"]["contents"][0]["parts"]
    assert len(parts) == 1  # 참고 이미지 없음, 안내문 텍스트뿐
    assert "아직 등장하면 안" in parts[0]["text"]
    assert "테스트 상품" in parts[0]["text"]


def test_generate_scene_image_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nrecoveredbytes"
    client = _FakeClient(
        [
            _FakeResponse(500, text="server error"),
            _FakeResponse(200, _success_body(expected)),
        ]
    )

    result, mime_type = generate_scene_image(
        "긴장감", "나레이션", "테스트 상품",
        product_reference=(b"reference", "image/jpeg"), client=client,
    )

    assert result == expected
    assert mime_type == "image/png"
    assert len(client.calls) == 2


def test_generate_scene_image_retries_twice_then_succeeds(monkeypatch):
    # AGENTS.md 기본 컨벤션(재시도 1회)보다 한 번 더 시도한다 — 실측(사용자 피드백)으로
    # 재시도 1회까지 연속 실패해 원본 리스팅 사진 폴백이 나온 사례가 있어 늘렸다.
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nrecoveredafter2bytes"
    client = _FakeClient(
        [
            _FakeResponse(500, text="server error 1"),
            _FakeResponse(500, text="server error 2"),
            _FakeResponse(200, _success_body(expected)),
        ]
    )

    result, mime_type = generate_scene_image(
        "긴장감", "나레이션", "테스트 상품",
        product_reference=(b"reference", "image/jpeg"), client=client,
    )

    assert result == expected
    assert mime_type == "image/png"
    assert len(client.calls) == 3


def test_generate_scene_image_raises_after_three_failures(monkeypatch):
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    client = _FakeClient(
        [_FakeResponse(500, text="err1"), _FakeResponse(500, text="err2"), _FakeResponse(500, text="err3")]
    )

    with pytest.raises(ImageGenerationError):
        generate_scene_image(
            "긴장감", "나레이션", "테스트 상품",
            product_reference=(b"reference", "image/jpeg"), client=client,
        )
    assert len(client.calls) == 3


def test_generate_scene_image_raises_when_response_has_no_image(monkeypatch):
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    no_image_body = {"candidates": [{"content": {"parts": [{"text": "그림을 못 그렸어요"}]}}]}
    client = _FakeClient([_FakeResponse(200, no_image_body)] * 3)

    with pytest.raises(ImageGenerationError):
        generate_scene_image(
            "긴장감", "나레이션", "테스트 상품",
            product_reference=(b"reference", "image/jpeg"), client=client,
        )


def test_generate_scene_image_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "")

    with pytest.raises(ImageGenerationError):
        generate_scene_image("긴장감", "나레이션", "테스트 상품", client=_FakeClient([]))


def test_generate_scene_image_includes_character_reference_when_given(monkeypatch):
    # 등장인물이 씬마다 바뀌는 문제 — 이전 씬 생성 결과를 참고 이미지로 함께 보내 같은
    # 인물을 유지하도록 요청해야 한다.
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nnewscenebytes"
    client = _FakeClient([_FakeResponse(200, _success_body(expected))])
    character_ref = (b"previous-scene-bytes", "image/png")

    generate_scene_image(
        "긴장감", "나레이션", "테스트 상품",
        product_reference=(b"reference", "image/jpeg"),
        character_reference=character_ref, client=client,
    )

    parts = client.calls[0]["json"]["contents"][0]["parts"]
    assert len(parts) == 3  # 안내문 + 상품 참고 이미지 + 인물 참고 이미지
    assert parts[2]["inlineData"]["mimeType"] == "image/png"
    assert "얼굴·헤어스타일" in parts[0]["text"]
    assert "실패로 간주" in parts[0]["text"]  # 포즈/구도까지 베끼면 안 된다는 지시
    # 회귀 테스트 — 후킹은 서양인으로 생성됐는데 이후 씬에서 동양인으로 바뀌는 등
    # 인종이 씬마다 달라지는 문제(사용자 피드백)가 있어 인종/피부톤 유지 지시를 추가했다.
    assert "인종" in parts[0]["text"]
    assert "피부톤" in parts[0]["text"]
    # 회귀 테스트 — 후킹은 여성으로 생성됐는데 이후 씬에서 남성으로 바뀌는 등 성별이
    # 씬마다 달라지는 문제(사용자 피드백, 2026-08-18)가 있어 성별 유지 지시를 추가했다.
    assert "성별" in parts[0]["text"]


def test_generate_scene_image_includes_character_reference_without_product_reference(monkeypatch):
    # pre-reveal 씬이라도 인물 일관성은 유지해야 한다 — 상품 참고 이미지만 빠지고 인물
    # 참고 이미지는 그대로 전달돼야 한다.
    monkeypatch.setattr(image_generator, "GEMINI_API_KEY", "fake-key")
    expected = b"\x89PNG\r\n\x1a\nprerevealbytes"
    client = _FakeClient([_FakeResponse(200, _success_body(expected))])
    character_ref = (b"previous-scene-bytes", "image/png")

    generate_scene_image(
        "긴장감", "나레이션", "테스트 상품", character_reference=character_ref, client=client,
    )

    parts = client.calls[0]["json"]["contents"][0]["parts"]
    assert len(parts) == 2  # 안내문 + 인물 참고 이미지 (상품 참고 이미지 없음)
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
