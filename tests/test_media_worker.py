"""후킹(첫 씬) 확인/재생성 기능(Phase 6) — worker.py의 새 job 종류 검증.

process_hook_preview_job은 DB 왕복(가짜 클라이언트)만 확인하면 되므로 ffmpeg가 필요
없다. render_script(hook_preview=...)와 process_hook_patch_job은 실제 ffmpeg로 조립까지
끝내야 검증 가치가 있어(회귀 가드 목적) tests/test_media_render_integration.py와 같은
방식으로 FFMPEG_AVAILABLE일 때만 실행한다. Gemini/Veo는 어느 테스트에서도 실제로 부르지
않는다 — build_scene_image/build_scene_video를 가짜로 바꿔치기한다.
"""

from __future__ import annotations

import os
import subprocess
from io import BytesIO

import pytest
from PIL import Image

from app.media import worker
from app.media.image_generator import ImageGenerationError
from app.media.render import RenderError, probe_duration_sec, resolve_font_path
from app.media.video_generator import VideoGenerationError

FFMPEG_AVAILABLE = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0


class _FakeQuery:
    def __init__(self, rows: list[dict], op: str, payload=None):
        self.rows = rows
        self.op = op
        self.payload = payload
        self.filters: list[tuple[str, object]] = []
        self._order_desc = False
        self._limit = None

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, _col, desc=False):
        self._order_desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def select(self, *_args):
        return self

    def _matches(self, row) -> bool:
        return all(row.get(col) == val for col, val in self.filters)

    def execute(self):
        if self.op == "select":
            data = [r for r in self.rows if self._matches(r)]
            if self._limit is not None:
                data = data[: self._limit]
            return type("Result", (), {"data": data})()
        if self.op == "update":
            matched = [r for r in self.rows if self._matches(r)]
            for r in matched:
                r.update(self.payload)
            return type("Result", (), {"data": matched})()
        raise NotImplementedError(self.op)


class FakeClient:
    """scripts/products/render_jobs 여러 테이블을 흉내내는 최소 fake."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self._current = None

    def table(self, name):
        self._current = name
        return self

    def select(self, *args):
        return _FakeQuery(self.tables[self._current], "select")

    def update(self, payload):
        return _FakeQuery(self.tables[self._current], "update", payload)


def _jpeg_bytes(color=(200, 120, 60)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (400, 300), color).save(buf, "JPEG")
    return buf.getvalue()


def _make_real_video(path: str, duration_sec: float = 2.0, color: str = "red") -> str:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:size=1080x1920:duration={duration_sec}:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", path,
        ],
        capture_output=True,
        check=True,
    )
    return path


def _make_silence_mp3(path: str, duration_sec: float) -> str:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(duration_sec), path],
        capture_output=True,
        check=True,
    )
    return path


# --- process_hook_preview_job (DB 왕복만 확인, ffmpeg 불필요) ---


def test_process_hook_preview_job_success_updates_script_and_job(monkeypatch, tmp_path):
    script = {
        "id": "script-1",
        "product_id": "product-1",
        "script_json": {"tone": "standard", "scenes": [{"seq": 1, "visual": "v", "narration": "n", "stage": "reveal"}]},
        "hook_preview_status": None,
    }
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    job = {"id": "job-1", "script_id": "script-1", "status": "queued", "kind": "hook_preview"}
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [job]})

    fake_image_path = str(tmp_path / "scene_1.jpg")

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        return fake_image_path, (b"bytes", "image/jpeg")

    def fake_build_scene_video(*_a, **_k):
        raise AssertionError("hook_preview는 더 이상 Veo(build_scene_video)를 호출하면 안 된다 — 렌더 시점에만 호출")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    result = worker.process_hook_preview_job("job-1", client=client, work_root=str(tmp_path / "renders"))

    assert result["status"] == "done"
    assert script["hook_preview_status"] == "done"
    assert script["hook_preview_image_path"] == fake_image_path
    # Veo 하루 호출 한도를 확인 단계에서 태우지 않기 위해 영상은 만들지 않는다
    # (2026-08-17~, 사용자 피드백) — 실제 후킹 영상은 최종 렌더링 때 딱 한 번만 만든다.
    assert script["hook_preview_video_path"] is None
    assert job["status"] == "done"


def test_process_hook_preview_job_marks_failed_when_image_generation_fails(monkeypatch, tmp_path):
    script = {
        "id": "script-1",
        "product_id": "product-1",
        "script_json": {"tone": "standard", "scenes": [{"seq": 1, "visual": "v", "narration": "n", "stage": "reveal"}]},
        "hook_preview_status": None,
    }
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    job = {"id": "job-1", "script_id": "script-1", "status": "queued", "kind": "hook_preview"}
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [job]})

    def fake_build_scene_image_fail(*_a, **_k):
        raise ImageGenerationError("Gemini 이미지 생성 실패 (status=500): ...")

    # hook_preview는 이미지만 만들므로, 남은 실패 경로는 이미지 생성 실패뿐이다.
    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image_fail)

    with pytest.raises(ImageGenerationError):
        worker.process_hook_preview_job("job-1", client=client, work_root=str(tmp_path / "renders"))

    assert script["hook_preview_status"] == "failed"
    assert job["status"] == "failed"
    assert "status=500" in job["error_message"]


def test_process_hook_preview_job_for_non_hook_scene_uses_hook_character_ref(monkeypatch, tmp_path):
    hook_image_path = str(tmp_path / "hook.jpg")
    Image.new("RGB", (400, 300), (10, 200, 10)).save(hook_image_path, "JPEG")

    script = {
        "id": "script-1",
        "product_id": "product-1",
        "script_json": {
            "tone": "standard",
            "scenes": [
                {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy"},
                {"seq": 2, "visual": "장면2", "narration": "n2", "stage": "cta"},
            ],
        },
        "hook_preview_status": "done",
        "hook_preview_image_path": hook_image_path,
        "hook_preview_video_path": str(tmp_path / "hook.mp4"),
        "scene_preview_images": {},
    }
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    job = {"id": "job-2", "script_id": "script-1", "status": "queued", "kind": "hook_preview", "target_seq": 2}
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [job]})

    received_character_refs = []
    fake_image_path = str(tmp_path / "scene_2.jpg")

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        received_character_refs.append(character_ref)
        return fake_image_path, character_ref

    def fake_build_scene_video(*_a, **_k):
        raise AssertionError("후킹이 아닌 씬은 Veo(build_scene_video)를 호출하면 안 된다")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    result = worker.process_hook_preview_job("job-2", client=client, work_root=str(tmp_path / "renders"))

    assert result == {"status": "done", "seq": 2}
    with open(hook_image_path, "rb") as f:
        expected_bytes = f.read()
    assert received_character_refs == [(expected_bytes, "image/jpeg")]
    assert script["scene_preview_images"]["2"] == {"image_path": fake_image_path, "status": "done"}
    # 후킹(scripts.hook_preview_*) 필드는 그대로 유지된다 — 다른 씬 생성이 건드리면 안 됨.
    assert script["hook_preview_image_path"] == hook_image_path
    assert job["status"] == "done"


def test_process_hook_preview_job_skips_character_ref_for_independent_character_scene(monkeypatch, tmp_path):
    """씬 visual에 "[다른인물]" 표시가 있으면 후킹 인물 참조를 넘기지 않는다 — 특정 씬만
    의도적으로 다른 사람(예: 다른 성별)으로 연출하고 싶을 때(사용자 피드백, 2026-08-18:
    성별을 지정해도 후킹 참조와 계속 충돌해 뜻대로 안 나옴) 쓰는 탈출구."""
    hook_image_path = str(tmp_path / "hook.jpg")
    Image.new("RGB", (400, 300), (10, 200, 10)).save(hook_image_path, "JPEG")

    script = {
        "id": "script-1",
        "product_id": "product-1",
        "script_json": {
            "tone": "standard",
            "scenes": [
                {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy"},
                {"seq": 2, "visual": "등 닦기 [다른인물] (남성)", "narration": "n2", "stage": "cta"},
            ],
        },
        "hook_preview_status": "done",
        "hook_preview_image_path": hook_image_path,
        "hook_preview_video_path": None,
        "scene_preview_images": {},
    }
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    job = {"id": "job-2", "script_id": "script-1", "status": "queued", "kind": "hook_preview", "target_seq": 2}
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [job]})

    received_character_refs = []
    fake_image_path = str(tmp_path / "scene_2.jpg")

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        received_character_refs.append(character_ref)
        return fake_image_path, character_ref

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)

    worker.process_hook_preview_job("job-2", client=client, work_root=str(tmp_path / "renders"))

    assert received_character_refs == [None]


def test_process_hook_preview_job_for_non_hook_scene_fails_when_hook_not_done(monkeypatch, tmp_path):
    script = {
        "id": "script-1",
        "product_id": "product-1",
        "script_json": {
            "tone": "standard",
            "scenes": [
                {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy"},
                {"seq": 2, "visual": "장면2", "narration": "n2", "stage": "cta"},
            ],
        },
        "hook_preview_status": None,
        "scene_preview_images": {},
    }
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    job = {"id": "job-2", "script_id": "script-1", "status": "queued", "kind": "hook_preview", "target_seq": 2}
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [job]})

    monkeypatch.setattr(worker, "build_scene_image", lambda *a, **k: (str(tmp_path / "x.jpg"), None))

    with pytest.raises(RenderError, match="후킹"):
        worker.process_hook_preview_job("job-2", client=client, work_root=str(tmp_path / "renders"))

    assert script["scene_preview_images"]["2"]["status"] == "failed"
    assert job["status"] == "failed"


# --- render_script(hook_preview=...): 씬0 재사용 + 캐릭터 참조 전파 (실제 ffmpeg 필요) ---


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_render_script_reuses_hook_preview_and_skips_veo(tmp_path, monkeypatch):
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    preview_image_path = str(preview_dir / "hook.jpg")
    Image.new("RGB", (400, 300), (10, 200, 10)).save(preview_image_path, "JPEG")
    preview_video_path = str(preview_dir / "hook.mp4")
    _make_real_video(preview_video_path, duration_sec=2.0)

    work_dir = str(tmp_path / "work")

    scenes = [
        {"seq": 1, "visual": "후킹 장면", "narration": "이거 실화임?", "stage": "empathy", "caption": "후킹", "duration_sec": 2.0},
        {"seq": 2, "visual": "마무리 장면", "narration": "지금 확인해보세요", "stage": "cta", "caption": "CTA", "duration_sec": 2.0},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}
    product = {"product_name": "테스트 상품", "image_urls": [], "category": None}

    image_calls = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        image_calls.append((scene["seq"], character_ref))
        path = os.path.join(work_dir, f"scene_{scene['seq']}.jpg")
        Image.new("RGB", (400, 300), (10, 10, 200)).save(path, "JPEG")
        return path, character_ref or (b"generated", "image/jpeg")

    def fake_build_scene_video(*_args, **_kwargs):
        raise AssertionError("hook_preview가 있으면 씬0에 대해 build_scene_video가 호출되면 안 된다")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    def fake_synthesize_script_audio(scenes, work_dir, client=None, pre_reveal_stages=None):
        results = []
        for scene in scenes:
            path = os.path.join(work_dir, f"audio_{scene['seq']}.mp3")
            _make_silence_mp3(path, 2.0)
            results.append({"seq": scene["seq"], "path": path, "duration_sec": 2.0})
        return results

    monkeypatch.setattr(worker, "synthesize_script_audio", fake_synthesize_script_audio)

    result = worker.render_script(
        script_json, product, work_dir,
        hook_preview={"image_path": preview_image_path, "video_path": preview_video_path},
    )

    assert os.path.exists(result["output_path"])
    # 씬0(후킹)은 build_scene_image를 거치지 않는다 — 미리보기 이미지를 그대로 재사용.
    assert [seq for seq, _ in image_calls] == [2]
    # 씬1(2번째 호출부)에 넘어간 character_ref는 hook_preview 이미지에서 만든 것이어야 한다.
    with open(preview_image_path, "rb") as f:
        expected_bytes = f.read()
    assert image_calls[0][1] == (expected_bytes, "image/jpeg")


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_render_script_generates_video_once_when_hook_preview_has_no_video(tmp_path, monkeypatch):
    """hook_preview는 이제 이미지만 만든다(2026-08-17~) — 그 스틸컷은 재사용하되, 영상은
    이 렌더링에서 딱 한 번 새로 만들어야 한다(Veo 하루 호출 한도를 확인 단계에서 미리
    태우지 않기 위한 설계 변경)."""
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    preview_image_path = str(preview_dir / "hook.jpg")
    Image.new("RGB", (400, 300), (10, 200, 10)).save(preview_image_path, "JPEG")

    work_dir = str(tmp_path / "work")
    scenes = [
        {"seq": 1, "visual": "후킹 장면", "narration": "이거 실화임?", "stage": "empathy", "caption": "후킹", "duration_sec": 2.0},
        {"seq": 2, "visual": "마무리 장면", "narration": "지금 확인해보세요", "stage": "cta", "caption": "CTA", "duration_sec": 2.0},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}
    product = {"product_name": "테스트 상품", "image_urls": [], "category": None}

    image_calls = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        image_calls.append(scene["seq"])
        path = os.path.join(work_dir, f"scene_{scene['seq']}.jpg")
        Image.new("RGB", (400, 300), (10, 10, 200)).save(path, "JPEG")
        return path, character_ref or (b"generated", "image/jpeg")

    video_calls = []

    def fake_build_scene_video(scene, image_path, work_dir):
        video_calls.append(scene["seq"])
        return _make_real_video(os.path.join(work_dir, "hook_video.mp4"), duration_sec=2.0)

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    def fake_synthesize_script_audio(scenes, work_dir, client=None, pre_reveal_stages=None):
        results = []
        for scene in scenes:
            path = os.path.join(work_dir, f"audio_{scene['seq']}.mp3")
            _make_silence_mp3(path, 2.0)
            results.append({"seq": scene["seq"], "path": path, "duration_sec": 2.0})
        return results

    monkeypatch.setattr(worker, "synthesize_script_audio", fake_synthesize_script_audio)

    result = worker.render_script(
        script_json, product, work_dir,
        hook_preview={"image_path": preview_image_path, "video_path": None},
    )

    assert os.path.exists(result["output_path"])
    # 씬0(후킹) 스틸컷은 재사용하고(build_scene_image 호출 안 함), 영상은 이번에 딱 한 번 만든다.
    assert image_calls == [2]
    assert video_calls == [1]


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_render_script_reuses_scene_previews_for_non_hook_scenes(tmp_path, monkeypatch):
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    scene2_preview_path = str(preview_dir / "scene2.jpg")
    Image.new("RGB", (400, 300), (250, 250, 10)).save(scene2_preview_path, "JPEG")

    work_dir = str(tmp_path / "work")
    scenes = [
        {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy", "caption": "후킹", "duration_sec": 2.0},
        {"seq": 2, "visual": "장면2", "narration": "n2", "stage": "cta", "caption": "CTA", "duration_sec": 2.0},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}
    product = {"product_name": "테스트 상품", "image_urls": [], "category": None}

    image_calls = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        image_calls.append(scene["seq"])
        path = os.path.join(work_dir, f"scene_{scene['seq']}.jpg")
        Image.new("RGB", (400, 300), (10, 10, 200)).save(path, "JPEG")
        return path, character_ref or (b"generated", "image/jpeg")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)

    def fake_build_scene_video_fail(*_a, **_k):
        raise VideoGenerationError("veo failed (test)")

    # 씬1(후킹)은 hook_preview가 없으니 정상적으로 Veo가 불리지만, 여기선 실패시켜 정지
    # 이미지+Ken Burns 폴백 경로를 검증한다.
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video_fail)

    def fake_synthesize_script_audio(scenes, work_dir, client=None, pre_reveal_stages=None):
        results = []
        for scene in scenes:
            path = os.path.join(work_dir, f"audio_{scene['seq']}.mp3")
            _make_silence_mp3(path, 2.0)
            results.append({"seq": scene["seq"], "path": path, "duration_sec": 2.0})
        return results

    monkeypatch.setattr(worker, "synthesize_script_audio", fake_synthesize_script_audio)

    result = worker.render_script(
        script_json, product, work_dir,
        scene_previews={2: scene2_preview_path},
    )

    assert os.path.exists(result["output_path"])
    # 씬2는 확정된 미리보기를 재사용해 build_scene_image를 거치지 않는다 — 씬1(후킹)만 호출.
    assert image_calls == [1]


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_render_script_skips_deleted_scenes(tmp_path, monkeypatch):
    """scene["deleted"]=True로 표시된 씬(프롬프트 확인 탭의 소프트 삭제)은 실제 렌더링에서
    빠져야 한다 — 목록엔 흔적을 남기되 최종 영상엔 안 들어가야 한다는 요청(사용자 피드백,
    2026-08-18)."""
    work_dir = str(tmp_path / "work")
    scenes = [
        {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy", "caption": "후킹", "duration_sec": 2.0},
        {
            "seq": 2,
            "visual": "삭제될 장면",
            "narration": "n2",
            "stage": "problem",
            "caption": "C2",
            "duration_sec": 2.0,
            "deleted": True,
        },
        {"seq": 3, "visual": "마무리", "narration": "n3", "stage": "cta", "caption": "C3", "duration_sec": 2.0},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}
    product = {"product_name": "테스트 상품", "image_urls": [], "category": None}

    built_seqs = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        built_seqs.append(scene["seq"])
        path = os.path.join(work_dir, f"scene_{scene['seq']}.jpg")
        Image.new("RGB", (400, 300), (10, 10, 200)).save(path, "JPEG")
        return path, character_ref or (b"generated", "image/jpeg")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", lambda *a, **k: (_ for _ in ()).throw(VideoGenerationError("skip")))

    def fake_synthesize_script_audio(scenes, work_dir, client=None, pre_reveal_stages=None):
        results = []
        for scene in scenes:
            path = os.path.join(work_dir, f"audio_{scene['seq']}.mp3")
            _make_silence_mp3(path, 2.0)
            results.append({"seq": scene["seq"], "path": path, "duration_sec": 2.0})
        return results

    monkeypatch.setattr(worker, "synthesize_script_audio", fake_synthesize_script_audio)

    result = worker.render_script(script_json, product, work_dir)

    assert os.path.exists(result["output_path"])
    assert built_seqs == [1, 3]  # 삭제된 씬2는 아예 호출되지 않는다
    assert [w["seq"] for w in result["scene_timeline"]] == [1, 3]


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_render_script_independent_character_scene_does_not_contaminate_later_scenes(tmp_path, monkeypatch):
    """[다른인물] 씬의 생성 결과가 그 다음 씬들의 character_ref로 이어지면 안 된다 —
    이어지면 예를 들어 후킹(여성) -> [다른인물](남성)까지는 의도대로 되더라도, 그 다음
    씬이 후킹이 아니라 방금 만든 남성 씬을 기준으로 이어져버려 뒤죽박죽이 된다."""
    work_dir = str(tmp_path / "work")
    scenes = [
        {"seq": 1, "visual": "후킹", "narration": "n1", "stage": "empathy", "caption": "후킹", "duration_sec": 2.0},
        {"seq": 2, "visual": "등 닦기 [다른인물]", "narration": "n2", "stage": "problem", "caption": "C2", "duration_sec": 2.0},
        {"seq": 3, "visual": "마무리", "narration": "n3", "stage": "cta", "caption": "C3", "duration_sec": 2.0},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}
    product = {"product_name": "테스트 상품", "image_urls": [], "category": None}

    received_character_refs = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        received_character_refs.append((scene["seq"], character_ref))
        path = os.path.join(work_dir, f"scene_{scene['seq']}.jpg")
        Image.new("RGB", (400, 300), (10, 10, 200)).save(path, "JPEG")
        # [다른인물] 씬(seq 2)은 참조 없이 불려서 새 캐릭터 이미지를 "발견"한 것처럼 굴지만,
        # 이게 다음 씬으로 전파되면 안 된다는 게 이 테스트의 핵심.
        return path, character_ref or (b"scene-%d-generated" % scene["seq"], "image/jpeg")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", lambda *a, **k: (_ for _ in ()).throw(VideoGenerationError("skip")))

    def fake_synthesize_script_audio(scenes, work_dir, client=None, pre_reveal_stages=None):
        results = []
        for scene in scenes:
            path = os.path.join(work_dir, f"audio_{scene['seq']}.mp3")
            _make_silence_mp3(path, 2.0)
            results.append({"seq": scene["seq"], "path": path, "duration_sec": 2.0})
        return results

    monkeypatch.setattr(worker, "synthesize_script_audio", fake_synthesize_script_audio)

    worker.render_script(script_json, product, work_dir)

    refs_by_seq = dict(received_character_refs)
    # 씬1(후킹)은 참조 없이 첫 생성.
    assert refs_by_seq[1] is None
    # 씬2([다른인물])는 후킹 참조를 받지 않는다.
    assert refs_by_seq[2] is None
    # 씬3은 씬2가 아니라 씬1(후킹)에서 나온 참조를 이어받아야 한다.
    assert refs_by_seq[3] == (b"scene-1-generated", "image/jpeg")


# --- process_hook_patch_job: 이미 완료된 렌더의 후킹만 교체 (실제 ffmpeg 필요) ---


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_process_hook_patch_job_replaces_only_hook_clip(tmp_path, monkeypatch):
    source_job_id = "source-job-1"
    source_work_dir = str(tmp_path / "renders" / source_job_id)
    os.makedirs(source_work_dir, exist_ok=True)
    font = resolve_font_path()

    # --- 원본 렌더가 이미 만들어 남긴 산출물을 흉내낸다 ---
    old_hook_video = _make_real_video(os.path.join(source_work_dir, "old_hook.mp4"), duration_sec=2.0, color="blue")
    audio1_path = os.path.join(source_work_dir, "scene_1_padded.mp3")
    _make_silence_mp3(audio1_path, 2.0)
    audio2_path = os.path.join(source_work_dir, "scene_2_sped.mp3")
    _make_silence_mp3(audio2_path, 2.0)

    from app.media.render import render_video_scene_clip, render_scene_clip

    render_video_scene_clip(old_hook_video, audio1_path, 2.0, "", os.path.join(source_work_dir, "clip_1.mp4"), source_work_dir, font_path=font)
    scene2_image = os.path.join(source_work_dir, "scene_2.jpg")
    Image.new("RGB", (400, 300), (200, 200, 10)).save(scene2_image, "JPEG")
    render_scene_clip(scene2_image, audio2_path, 2.0, "", os.path.join(source_work_dir, "clip_2.mp4"), source_work_dir, font_path=font)
    original_clip2_mtime = os.path.getmtime(os.path.join(source_work_dir, "clip_2.mp4"))

    scenes = [
        {"seq": 1, "visual": "후킹", "narration": "이거 실화임?", "stage": "empathy", "caption": "후킹"},
        {"seq": 2, "visual": "마무리", "narration": "지금 확인해보세요", "stage": "cta", "caption": "CTA"},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}

    script = {"id": "script-1", "product_id": "product-1", "script_json": script_json}
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    source_job = {"id": source_job_id, "script_id": "script-1", "status": "done", "kind": "full"}
    patch_job = {"id": "patch-job-1", "script_id": "script-1", "status": "queued", "kind": "hook_patch", "source_render_job_id": source_job_id}
    client = FakeClient(
        {"scripts": [script], "products": [product], "render_jobs": [source_job, patch_job]}
    )

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        assert scene["seq"] == 1, "hook_patch는 씬0(후킹)만 다시 생성해야 한다"
        path = os.path.join(work_dir, f"scene_{scene['seq']}_new.jpg")
        Image.new("RGB", (400, 300), (10, 250, 10)).save(path, "JPEG")
        return path, (b"new", "image/jpeg")

    def fake_build_scene_video(scene, image_path, work_dir):
        assert scene["seq"] == 1
        return _make_real_video(os.path.join(work_dir, "new_hook.mp4"), duration_sec=2.0, color="green")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    result = worker.process_hook_patch_job(
        "patch-job-1", client=client, work_root=str(tmp_path / "renders")
    )

    assert result["status"] == "done"
    assert patch_job["status"] == "done"
    # 다른 씬(2번)의 이미지 생성은 전혀 호출되지 않았다 — build_scene_image는 씬1만 호출.
    assert source_job["output_path"] == result["output_path"]
    assert os.path.exists(source_job["output_path"])
    assert os.path.exists(source_job["base_video_path"])
    # 씬2 clip은 다시 만들지 않았다(파일이 그대로 재사용됐다).
    assert os.path.getmtime(os.path.join(source_work_dir, "clip_2.mp4")) == original_clip2_mtime
    # 최종 결과물 길이는 두 씬(각 2초 안팎) 합계와 크게 어긋나지 않아야 한다.
    total_duration = probe_duration_sec(result["output_path"])
    assert 3.0 <= total_duration <= 5.0


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available in this environment")
def test_process_hook_patch_job_for_non_hook_scene_uses_image_only_and_hook_character_ref(tmp_path, monkeypatch):
    source_job_id = "source-job-2"
    source_work_dir = str(tmp_path / "renders" / source_job_id)
    os.makedirs(source_work_dir, exist_ok=True)
    font = resolve_font_path()

    # --- 원본 렌더가 이미 만들어 남긴 산출물을 흉내낸다(씬1=후킹 영상, 씬2=마지막 씬) ---
    hook_video = _make_real_video(os.path.join(source_work_dir, "hook.mp4"), duration_sec=2.0, color="blue")
    hook_image_path = os.path.join(source_work_dir, "scene_1.jpg")
    Image.new("RGB", (400, 300), (30, 30, 30)).save(hook_image_path, "JPEG")
    audio1_path = os.path.join(source_work_dir, "scene_1_padded.mp3")
    _make_silence_mp3(audio1_path, 2.0)
    audio2_path = os.path.join(source_work_dir, "scene_2_sped.mp3")
    _make_silence_mp3(audio2_path, 2.0)

    from app.media.render import render_video_scene_clip, render_scene_clip

    render_video_scene_clip(hook_video, audio1_path, 2.0, "", os.path.join(source_work_dir, "clip_1.mp4"), source_work_dir, font_path=font)
    original_clip1_mtime = os.path.getmtime(os.path.join(source_work_dir, "clip_1.mp4"))
    old_scene2_image = os.path.join(source_work_dir, "scene_2.jpg")
    Image.new("RGB", (400, 300), (200, 200, 10)).save(old_scene2_image, "JPEG")
    render_scene_clip(old_scene2_image, audio2_path, 2.0, "", os.path.join(source_work_dir, "clip_2.mp4"), source_work_dir, font_path=font)

    scenes = [
        {"seq": 1, "visual": "후킹", "narration": "이거 실화임?", "stage": "empathy", "caption": "후킹"},
        {"seq": 2, "visual": "마무리", "narration": "지금 확인해보세요", "stage": "cta", "caption": "CTA"},
    ]
    script_json = {"tone": "standard", "scenes": scenes, "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로 수수료를 제공받습니다."}

    script = {"id": "script-2", "product_id": "product-1", "script_json": script_json}
    product = {"id": "product-1", "product_name": "테스트 상품", "image_urls": [], "category": None}
    source_job = {"id": source_job_id, "script_id": "script-2", "status": "done", "kind": "full"}
    patch_job = {
        "id": "patch-job-2", "script_id": "script-2", "status": "queued", "kind": "hook_patch",
        "source_render_job_id": source_job_id, "target_seq": 2,
    }
    client = FakeClient({"scripts": [script], "products": [product], "render_jobs": [source_job, patch_job]})

    received_character_refs = []

    def fake_build_scene_image(scene, product, script_json, work_dir, character_ref=None):
        assert scene["seq"] == 2, "target_seq=2 job은 씬2만 다시 생성해야 한다"
        received_character_refs.append(character_ref)
        path = os.path.join(work_dir, "scene_2_new.jpg")
        Image.new("RGB", (400, 300), (10, 250, 10)).save(path, "JPEG")
        return path, character_ref

    def fake_build_scene_video(*_a, **_k):
        raise AssertionError("후킹이 아닌 씬은 Veo(build_scene_video)를 호출하면 안 된다")

    monkeypatch.setattr(worker, "build_scene_image", fake_build_scene_image)
    monkeypatch.setattr(worker, "build_scene_video", fake_build_scene_video)

    result = worker.process_hook_patch_job("patch-job-2", client=client, work_root=str(tmp_path / "renders"))

    assert result["status"] == "done"
    with open(hook_image_path, "rb") as f:
        expected_bytes = f.read()
    # 인물 참조는 이 render_job 자신의 후킹 이미지(scene_1.jpg)에서 고정해 가져와야 한다.
    assert received_character_refs == [(expected_bytes, "image/jpeg")]
    # 후킹(씬1) clip은 건드리지 않았다.
    assert os.path.getmtime(os.path.join(source_work_dir, "clip_1.mp4")) == original_clip1_mtime
    assert os.path.exists(source_job["output_path"])
