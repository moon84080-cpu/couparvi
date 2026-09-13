import subprocess

import pytest

from app.media import video_generator
from app.media.video_generator import (
    VideoGenerationError,
    _snap_duration_seconds,
    _validate_video_bytes,
    generate_scene_video,
)

FFMPEG_AVAILABLE = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b"", text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.content = content
        self.text = text

    def json(self):
        return self._json_body


class _FakeClient:
    def __init__(self, post_responses=None, get_responses=None):
        self._post_responses = list(post_responses or [])
        self._get_responses = list(get_responses or [])
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, params=None, json=None):
        self.post_calls.append({"url": url, "params": params, "json": json})
        return self._post_responses.pop(0)

    def get(self, url, params=None, follow_redirects=None):
        self.get_calls.append({"url": url, "params": params, "follow_redirects": follow_redirects})
        return self._get_responses.pop(0)


def _submit_ok(op_name="models/veo-3.1-fast-generate-preview/operations/abc123"):
    return _FakeResponse(200, {"name": op_name})


def _done_response(video_uri="https://generativelanguage.googleapis.com/v1beta/files/xyz:download?alt=media"):
    return _FakeResponse(
        200,
        {
            "done": True,
            "response": {
                "generateVideoResponse": {"generatedSamples": [{"video": {"uri": video_uri}}]}
            },
        },
    )


def _not_done_response():
    return _FakeResponse(200, {"done": False})


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # POLL_INTERVAL_SEC(10초)만큼 실제로 자면 테스트가 느려지니 무력화한다.
    monkeypatch.setattr(video_generator.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generator, "GEMINI_API_KEY", "fake-key")
    # 이 테스트들은 제출/폴링/다운로드/재시도 HTTP 흐름만 검증한다 — 실제 mp4가 아닌
    # 가짜 바이트를 "다운로드된 영상"으로 쓰므로, _validate_video_bytes(ffprobe 검증)는
    # 여기서는 항상 통과시킨다. 검증 로직 자체는 아래 별도 테스트에서 다룬다.
    monkeypatch.setattr(video_generator, "_validate_video_bytes", lambda *_a, **_k: None)


def test_generate_scene_video_returns_bytes_on_first_poll():
    client = _FakeClient(
        post_responses=[_submit_ok()],
        get_responses=[_done_response(), _FakeResponse(200, content=b"fakevideobytes")],
    )

    result = generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)

    assert result == b"fakevideobytes"
    assert len(client.post_calls) == 1
    sent_body = client.post_calls[0]["json"]
    assert sent_body["instances"][0]["image"]["mimeType"] == "image/jpeg"
    assert sent_body["parameters"]["aspectRatio"] == "9:16"
    # duration_sec을 안 넘기면 Veo가 기본값(8초)으로 생성해 대본이 의도한 길이보다 훨씬
    # 길어지는 문제(사용자 피드백)가 있었다 — 항상 명시적으로 durationSeconds를 보낸다.
    # 반드시 숫자 타입이어야 한다 — 문자열로 보내면 Veo가 400으로 거부한다(실제 API로 확인).
    assert sent_body["parameters"]["durationSeconds"] == 4
    assert isinstance(sent_body["parameters"]["durationSeconds"], int)


def test_snap_duration_seconds_rounds_to_nearest_allowed_value():
    assert _snap_duration_seconds(None) == 4
    assert _snap_duration_seconds(3) == 4
    assert _snap_duration_seconds(5) == 4
    assert _snap_duration_seconds(6) == 6
    assert _snap_duration_seconds(7) == 6
    assert _snap_duration_seconds(9) == 8


def test_generate_scene_video_forwards_duration_sec_to_request():
    client = _FakeClient(
        post_responses=[_submit_ok()],
        get_responses=[_done_response(), _FakeResponse(200, content=b"fakevideobytes")],
    )

    generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", duration_sec=3, client=client)

    sent_body = client.post_calls[0]["json"]
    assert sent_body["parameters"]["durationSeconds"] == 4


def test_generate_scene_video_polls_until_done():
    client = _FakeClient(
        post_responses=[_submit_ok()],
        get_responses=[_not_done_response(), _not_done_response(), _done_response(), _FakeResponse(200, content=b"video-bytes")],
    )

    result = generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)

    assert result == b"video-bytes"
    assert len(client.get_calls) == 4  # 폴링 3회 + 다운로드 1회


def test_generate_scene_video_retries_once_then_succeeds():
    # 후킹(첫 씬)은 반드시 영상으로 나가야 해서(사용자 피드백), Veo의 비결정적 실패(콘텐츠
    # 안전 필터 등)에 대비해 AGENTS.md 컨벤션대로 재시도 1회를 거친다.
    client = _FakeClient(
        post_responses=[_FakeResponse(500, text="server error"), _submit_ok()],
        get_responses=[_done_response(), _FakeResponse(200, content=b"recovered-bytes")],
    )

    result = generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)

    assert result == b"recovered-bytes"
    assert len(client.post_calls) == 2


def test_generate_scene_video_raises_on_submit_failure_after_retry():
    client = _FakeClient(post_responses=[_FakeResponse(500, text="server error"), _FakeResponse(500, text="err2")])

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)


def test_generate_scene_video_raises_on_poll_failure_after_retry():
    client = _FakeClient(
        post_responses=[_submit_ok(), _submit_ok()],
        get_responses=[_FakeResponse(500, text="poll error"), _FakeResponse(500, text="poll error 2")],
    )

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)


def test_generate_scene_video_raises_when_done_response_missing_video_after_retry():
    client = _FakeClient(
        post_responses=[_submit_ok(), _submit_ok()],
        get_responses=[
            _FakeResponse(200, {"done": True, "response": {}}),
            _FakeResponse(200, {"done": True, "response": {}}),
        ],
    )

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)


def test_generate_scene_video_raises_on_download_failure_after_retry():
    client = _FakeClient(
        post_responses=[_submit_ok(), _submit_ok()],
        get_responses=[_done_response(), _FakeResponse(500), _done_response(), _FakeResponse(500)],
    )

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)


def test_generate_scene_video_raises_after_max_poll_attempts_on_both_tries(monkeypatch):
    monkeypatch.setattr(video_generator, "MAX_POLL_ATTEMPTS", 3)
    client = _FakeClient(
        post_responses=[_submit_ok(), _submit_ok()],
        get_responses=[
            _not_done_response(), _not_done_response(), _not_done_response(),
            _not_done_response(), _not_done_response(), _not_done_response(),
        ],
    )

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)


def test_generate_scene_video_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(video_generator, "GEMINI_API_KEY", "")

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=_FakeClient())


# --- _validate_video_bytes: Veo가 깨진/손상된 파일을 돌려주는 명백한 실패를 걸러낸다
# (사용자 피드백: 재렌더 시 후킹 영상이 이상하게 나오는 경우가 있었다) ---


def test_validate_video_bytes_rejects_too_small_file():
    with pytest.raises(VideoGenerationError, match="용량"):
        _validate_video_bytes(b"tiny")


def test_validate_video_bytes_rejects_corrupted_container():
    # 크기는 충분하지만 mp4 컨테이너가 아닌 쓰레기 바이트 — ffprobe가 읽지 못해야 한다.
    with pytest.raises(VideoGenerationError, match="검증 실패"):
        _validate_video_bytes(b"not a real video file" * 100)


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_validate_video_bytes_accepts_real_video(tmp_path):
    real_video_path = tmp_path / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(real_video_path),
        ],
        capture_output=True,
        check=True,
    )
    real_video_bytes = real_video_path.read_bytes()
    _validate_video_bytes(real_video_bytes)  # 예외 없이 통과해야 한다


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_generate_scene_video_retries_when_downloaded_video_is_corrupted(monkeypatch):
    # 위 autouse 픽스처의 _validate_video_bytes 무력화를 이 테스트에서만 되돌려서, 실제
    # 검증 로직이 재시도 흐름과 제대로 연결돼 있는지 end-to-end로 확인한다.
    monkeypatch.undo()
    monkeypatch.setattr(video_generator.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generator, "GEMINI_API_KEY", "fake-key")

    client = _FakeClient(
        post_responses=[_submit_ok(), _submit_ok()],
        get_responses=[
            _done_response(), _FakeResponse(200, content=b"garbage-not-a-video"),
            _done_response(), _FakeResponse(200, content=b"garbage-not-a-video"),
        ],
    )

    with pytest.raises(VideoGenerationError):
        generate_scene_video(b"reference-image", "image/jpeg", "고민", "나레이션", client=client)
    assert len(client.post_calls) == 2  # 검증 실패도 재시도 대상이 된다
