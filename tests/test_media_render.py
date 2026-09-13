import os

from app.media.render import (
    HOOK_FONTSIZE,
    MAX_NARRATION_SPEED,
    MIN_NARRATION_SPEED,
    NARRATION_SPEED,
    build_animated_caption_filter,
    build_hook_text_filter,
    build_scene_filter_complex,
    build_text_block_filter,
    build_video_scene_filter_complex,
    build_xfade_filter_complex,
    build_zoompan_filter,
    calc_narration_speed,
    compute_scene_windows,
    escape_path_for_filter,
    pick_bgm_track,
    predict_scene_duration_sec,
    recomposite_captions,
    resolve_font_path,
    resolve_scene_duration_sec,
    resolve_font_path_for_key,
    resolve_scene_text_elements,
    wrap_text_lines,
)


def test_escape_path_for_filter_handles_windows_drive_colon():
    escaped = escape_path_for_filter("C:\\work\\couparvi\\caption.txt")
    assert escaped == "C\\:/work/couparvi/caption.txt"


def test_escape_path_for_filter_handles_single_quote():
    escaped = escape_path_for_filter("C:/it's/a/path.txt")
    assert "\\'" in escaped


def test_build_zoompan_filter_frame_count_matches_duration():
    filt = build_zoompan_filter(2.0, fps=30)
    assert "d=60" in filt
    assert "s=1080x1920" in filt
    assert "fps=30" in filt


def test_build_zoompan_filter_minimum_one_frame():
    filt = build_zoompan_filter(0.0, fps=30)
    assert "d=1" in filt


def test_build_text_block_filter_contains_textfile_and_font_per_line():
    filt = build_text_block_filter(["line0.txt", "line1.txt"], "font.ttf", bottom_margin=360, fontsize=54)
    assert "textfile='line0.txt'" in filt
    assert "textfile='line1.txt'" in filt
    assert "fontfile='font.ttf'" in filt
    assert "fontcolor=white" in filt
    assert filt.count("drawtext=") == 2


def test_build_text_block_filter_has_no_background_box():
    # 반투명 배경 박스가 화면을 답답하게 가린다는 피드백으로 제거하고, 대신 글자 자체에
    # 외곽선을 둘러 배경 없이도 가독성을 확보한다.
    filt = build_text_block_filter(["line0.txt"], "font.ttf", bottom_margin=360, fontsize=54)
    assert "drawbox=" not in filt
    assert "bordercolor=black" in filt
    assert "borderw=" in filt


def test_build_text_block_filter_line_y_offset_scales_with_line_height():
    filt = build_text_block_filter(
        ["a.txt", "b.txt", "c.txt"], "font.ttf", bottom_margin=360, fontsize=54, line_height=42
    )
    # 3줄, line_height=42 -> block_height=126, 각 줄 y는 h-360-126+{0,42,84}
    assert "h-360-126+0" in filt
    assert "h-360-126+42" in filt
    assert "h-360-126+84" in filt


def test_build_text_block_filter_enable_expr_applied_to_every_line():
    filt = build_text_block_filter(
        ["d.txt", "e.txt"], "f.ttf", bottom_margin=140, fontsize=34, enable="gte(t,3)"
    )
    assert filt.count("enable='gte(t,3)'") == 2


def test_build_text_block_filter_no_literal_newline_char_in_output():
    # \n을 그대로 drawtext 하나에 넘기면 ffmpeg가 개행문자 자체를 tofu 글리프로 그리는
    # 버그가 있어(수동 렌더링으로 확인), 줄마다 별도 파일+drawtext로 쪼갠다.
    filt = build_text_block_filter(["line0.txt", "line1.txt"], "font.ttf", bottom_margin=360, fontsize=54)
    assert "\\n" not in filt


def test_build_scene_filter_complex_without_disclosure(tmp_path):
    font = resolve_font_path()
    filt = build_scene_filter_complex(5.0, "안녕 반가워요", font, str(tmp_path), "scene1")
    assert "[0:v]" in filt
    assert "[outv]" in filt
    assert "disc" not in filt


def test_build_scene_filter_complex_with_disclosure_has_enable_gate(tmp_path):
    font = resolve_font_path()
    filt = build_scene_filter_complex(
        10.0, "안녕 반가워요", font, str(tmp_path), "scene1", disclosure_line_paths=["disc.txt"]
    )
    assert "disc.txt" in filt
    assert "enable=" in filt
    assert "gte(t,8.0)" in filt  # 10초 씬의 마지막 2초부터 노출


def test_build_video_scene_filter_complex_has_no_zoompan(tmp_path):
    # 실제 영상(Veo) 클립엔 이미 움직임이 있어 정지 이미지용 Ken Burns(zoompan)를 쓰면 안 된다.
    font = resolve_font_path()
    filt = build_video_scene_filter_complex("안녕 반가워요", font, str(tmp_path), "scene1")
    assert "zoompan" not in filt
    assert "[0:v]" in filt
    assert "[outv]" in filt


def test_build_video_scene_filter_complex_scales_to_canvas_size(tmp_path):
    font = resolve_font_path()
    filt = build_video_scene_filter_complex("안녕 반가워요", font, str(tmp_path), "scene1")
    assert "scale=1080:1920" in filt


def test_build_video_scene_filter_complex_with_hook_text_no_caption(tmp_path):
    # 첫 씬은 하단 자막을 생략하고 상단 후킹 문구만 넣을 수 있어야 한다.
    font = resolve_font_path()
    filt = build_video_scene_filter_complex("", font, str(tmp_path), "scene1", hook_text="이거 실화냐")
    assert "scene1.hook.line0.txt" in filt
    assert "scene1.caption.line0.txt" not in filt


def test_build_animated_caption_filter_has_background_drawbox(tmp_path):
    font = resolve_font_path()
    filt = build_animated_caption_filter("안녕 반가워요", font, str(tmp_path), "scene1")
    assert filt.startswith("drawbox=")


def test_build_animated_caption_filter_one_drawtext_per_line_not_per_word(tmp_path):
    # 문장이 한 줄에 들어가면(안 넘치면) 단어 개수와 무관하게 drawtext는 1개만 나와야 한다
    # — 단어별로 나눠 그리던 예전 방식(노래방 자막)과 달리, 이제 줄 단위로 한 번에 그린다.
    font = resolve_font_path()
    filt = build_animated_caption_filter("안녕 반가워요 오늘도", font, str(tmp_path), "scene1")
    assert filt.count("drawtext=") == 1


def test_build_animated_caption_filter_no_per_word_reveal_timing(tmp_path):
    # 단어가 하나씩 늘어나며 나타나던 between(t,...) 타이밍 로직이 더는 없어야 한다.
    font = resolve_font_path()
    filt = build_animated_caption_filter("안녕 반가워요", font, str(tmp_path), "scene1")
    assert "between(t," not in filt


def test_build_animated_caption_filter_fades_in_from_scene_start(tmp_path):
    # 문장 전체가 씬 시작(t=0)부터 페이드인해서 계속 유지돼야 한다.
    font = resolve_font_path()
    filt = build_animated_caption_filter("안녕 반가워요", font, str(tmp_path), "scene1")
    assert "alpha='min(t/0.12,1)'" in filt


def test_build_animated_caption_filter_writes_full_line_text_to_file(tmp_path):
    font = resolve_font_path()
    build_animated_caption_filter("안녕 반가워요", font, str(tmp_path), "scene1")
    written = (tmp_path / "scene1.caption.line0.txt").read_text(encoding="utf-8")
    assert written == "안녕 반가워요"


def test_calc_narration_speed_matches_target_within_clamp_range():
    # 실제 6초 오디오를 목표 5초에 맞추려면 1.2배가 필요하고, clamp 범위(0.95~1.3) 안이라 그대로 쓰인다.
    speed = calc_narration_speed(actual_duration_sec=6.0, target_duration_sec=5.0)
    assert speed == 1.2


def test_calc_narration_speed_clamps_when_target_much_shorter_than_actual():
    # 10초짜리 오디오를 2초에 맞추려면 5배가 필요하지만 부자연스러워 MAX로 clamp된다.
    speed = calc_narration_speed(actual_duration_sec=10.0, target_duration_sec=2.0)
    assert speed == MAX_NARRATION_SPEED


def test_calc_narration_speed_clamps_when_target_much_longer_than_actual():
    # 2초짜리 오디오를 10초로 늘리려면 0.2배가 필요하지만 부자연스러워 MIN으로 clamp된다.
    speed = calc_narration_speed(actual_duration_sec=2.0, target_duration_sec=10.0)
    assert speed == MIN_NARRATION_SPEED


def test_calc_narration_speed_short_cta_line_does_not_slow_down_much():
    # 실측 회귀 테스트: "제품정보는 본문에 있어요, 확인해보세요." 실제 TTS 길이 3.34초를
    # 대본이 잡은 4초에 맞추려면 0.835배가 필요했는데, 예전 하한(0.85)에 걸려도 여전히
    # 눈에 띄게 느렸다는 피드백으로 하한을 0.95로 좁혔다.
    speed = calc_narration_speed(actual_duration_sec=3.34, target_duration_sec=4.0)
    assert speed == MIN_NARRATION_SPEED
    assert speed >= 0.95


def test_calc_narration_speed_falls_back_when_target_missing_or_invalid():
    assert calc_narration_speed(actual_duration_sec=5.0, target_duration_sec=None) == NARRATION_SPEED
    assert calc_narration_speed(actual_duration_sec=5.0, target_duration_sec=0) == NARRATION_SPEED
    assert calc_narration_speed(actual_duration_sec=5.0, target_duration_sec=-1) == NARRATION_SPEED


# 실측 회귀 테스트(사용자 피드백, 2026-08-19) — "라이브박스 2세대" 대본에서 씬별로 설정한
# duration_sec과 실제 렌더링 결과가 최대 63% 차이 났는데 사용자가 미리 알 방법이 없었다.
# predict_scene_duration_sec()는 나레이션 글자 수로 자연스러운 발화 길이를 추정해 같은
# clamp를 미리 적용함으로써, 렌더링 전에 이 차이를 예측할 수 있어야 한다.


def test_predict_scene_duration_sec_flags_target_much_shorter_than_natural_pace():
    # 34자를 3초에 (초당 11.3자 — AI 생성 기준 초당 5~6자보다 훨씬 빠름) 요청한 씬.
    # 실측: 실제 렌더링에서 3초 목표가 4.89초로 나왔다(요청 대비 +63%).
    narration = "야외나 차 안에서 맛있는 에스프레소 진짜 절실할 때 많으셨죠?"
    predicted = predict_scene_duration_sec(narration, target_duration_sec=3)
    assert predicted > 4.0  # 목표(3초)보다 1초 이상 길게 예측돼야 경고 대상이 된다


def test_predict_scene_duration_sec_flags_target_much_longer_than_natural_pace():
    # 20자를 5초에 (초당 4자 — 너무 느긋함) 요청한 씬.
    # 실측: 실제 렌더링에서 5초 목표가 3.08초로 나왔다(요청 대비 -38%).
    narration = "제품 정보는 고정 댓글을 확인하세요!"
    predicted = predict_scene_duration_sec(narration, target_duration_sec=5)
    assert predicted < 4.0  # 목표(5초)보다 1초 이상 짧게 예측돼야 경고 대상이 된다


def test_predict_scene_duration_sec_matches_target_when_pace_is_natural():
    # 49자를 7초에 (초당 7자 — AI 생성 기준과 크게 다르지 않음) 요청한 씬은 clamp에 안 걸려야 한다.
    narration = "캔커피는 미지근하고 밍밍한 데다, 카페 찾아 헤매다 보면 이미 쉬는 시간"
    predicted = predict_scene_duration_sec(narration, target_duration_sec=7)
    assert abs(predicted - 7) < 1.0  # 경고 임계값(1초) 미만이어야 한다


def test_predict_scene_duration_sec_returns_zero_for_empty_narration():
    assert predict_scene_duration_sec("", target_duration_sec=5) == 0.0


# resolve_scene_duration_sec() — 경고에서 한 걸음 더 나아가, 차이가 크면 대본(나레이션)
# 기준으로 duration_sec을 자동 재설정한다(사용자 피드백, 2026-08-19).


def test_resolve_scene_duration_sec_overrides_when_mismatch_is_large():
    narration = "야외나 차 안에서 맛있는 에스프레소 진짜 절실할 때 많으셨죠?"
    resolved = resolve_scene_duration_sec(narration, 3)
    assert resolved != 3
    assert resolved > 4


def test_resolve_scene_duration_sec_keeps_value_when_mismatch_is_small():
    narration = "캔커피는 미지근하고 밍밍한 데다, 카페 찾아 헤매다 보면 이미 쉬는 시간"
    assert resolve_scene_duration_sec(narration, 7) == 7


def test_resolve_scene_duration_sec_passes_through_missing_inputs():
    assert resolve_scene_duration_sec("나레이션", None) is None
    assert resolve_scene_duration_sec("", 5) == 5


def test_pick_bgm_track_returns_none_when_dir_missing(tmp_path):
    missing_dir = str(tmp_path / "no_such_dir")
    assert pick_bgm_track(missing_dir) is None


def test_pick_bgm_track_returns_none_when_dir_empty(tmp_path):
    empty_dir = tmp_path / "bgm"
    empty_dir.mkdir()
    assert pick_bgm_track(str(empty_dir)) is None


def test_pick_bgm_track_returns_a_file_when_present(tmp_path):
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir()
    track = bgm_dir / "track1.mp3"
    track.write_bytes(b"fake")
    picked = pick_bgm_track(str(bgm_dir))
    assert picked == str(track)


def test_resolve_font_path_finds_a_font():
    # 이 리포의 FONT_SEARCH_PATHS에는 Windows 시스템 폰트(malgun.ttf) 폴백이 있어
    # 로컬 개발 환경에서는 항상 찾아져야 한다. Phase 4에서 assets/fonts/ 번들 폰트로 교체될 예정.
    path = resolve_font_path()
    assert os.path.exists(path)


def test_wrap_text_lines_short_text_stays_one_line():
    font = resolve_font_path()
    lines = wrap_text_lines("짧은 문장", font, 54, max_width_px=900)
    assert lines == ["짧은 문장"]


def test_wrap_text_lines_long_disclosure_wraps_within_max_width():
    # 실제 렌더링에서 프레임 밖으로 삐져나갔던 고지문구로 회귀 테스트한다.
    font = resolve_font_path()
    disclosure = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    max_width = 972  # DISCLOSURE_MAX_WIDTH와 동일한 값
    lines = wrap_text_lines(disclosure, font, 34, max_width_px=max_width)

    assert len(lines) > 1
    from PIL import ImageFont

    pil_font = ImageFont.truetype(font, 34)
    for line in lines:
        assert pil_font.getlength(line) <= max_width


def test_wrap_text_lines_respects_manual_newline():
    font = resolve_font_path()
    lines = wrap_text_lines("첫째 줄\n둘째 줄", font, 54, max_width_px=900)
    assert lines == ["첫째 줄", "둘째 줄"]


def test_build_hook_text_filter_writes_lines_and_draws_text(tmp_path):
    font = resolve_font_path()
    filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene1")
    assert "drawtext=" in filt
    assert "bordercolor=black" in filt


def test_build_hook_text_filter_without_product_name_has_single_drawtext(tmp_path):
    font = resolve_font_path()
    filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene1")
    assert filt.count("drawtext=") == 1


def test_build_hook_text_filter_with_product_name_adds_second_line(tmp_path):
    # 3단 레이아웃 절충안(사용자 피드백): 상단에 후킹 문구 + 제품명을 함께 노출.
    font = resolve_font_path()
    filt = build_hook_text_filter(
        "이거 실화냐", font, str(tmp_path), "scene1", product_name="테스트 상품 20L"
    )
    assert filt.count("drawtext=") == 2
    assert "scene1.hook_product.txt" in filt
    with open(os.path.join(str(tmp_path), "scene1.hook_product.txt"), encoding="utf-8") as f:
        assert f.read() == "테스트 상품 20L"


def test_build_hook_text_filter_fontsize_override_scales_line_height(tmp_path):
    font = resolve_font_path()
    default_filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene1")
    assert f"fontsize={HOOK_FONTSIZE}" in default_filt

    big_filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene2", fontsize=100)
    assert "fontsize=100" in big_filt


def test_build_hook_text_filter_fontcolor_override(tmp_path):
    font = resolve_font_path()
    default_filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene1")
    assert "fontcolor=white" in default_filt

    override_filt = build_hook_text_filter("이거 실화냐", font, str(tmp_path), "scene2", fontcolor="#00ff00")
    assert "fontcolor=#00ff00" in override_filt


def test_build_animated_caption_filter_fontcolor_override(tmp_path):
    font = resolve_font_path()
    default_filt = build_animated_caption_filter("자막", font, str(tmp_path), "scene1")
    assert "fontcolor=0xFFE600" in default_filt

    override_filt = build_animated_caption_filter("자막", font, str(tmp_path), "scene2", fontcolor="#123456")
    assert "fontcolor=#123456" in override_filt


def test_build_text_block_filter_supports_custom_fontcolor_and_outline(tmp_path):
    filt = build_text_block_filter(
        ["line0.txt"], "font.ttf", bottom_margin=190, fontsize=32,
        fontcolor="0xFFE600", outline_width=3,
    )
    assert "fontcolor=0xFFE600" in filt
    assert "borderw=3" in filt


def test_build_scene_filter_complex_with_sticky_cta_text(tmp_path):
    font = resolve_font_path()
    filt = build_scene_filter_complex(
        5.0, "안녕 반가워요", font, str(tmp_path), "scene2", sticky_cta_text="지금 확인해보세요"
    )
    assert "scene2.sticky_cta.line0.txt" in filt


def test_build_video_scene_filter_complex_with_sticky_cta_text(tmp_path):
    font = resolve_font_path()
    filt = build_video_scene_filter_complex(
        "안녕 반가워요", font, str(tmp_path), "scene1", sticky_cta_text="지금 확인해보세요"
    )
    assert "scene1.sticky_cta.line0.txt" in filt


def test_build_xfade_filter_complex_chains_all_clips():
    filt, v_label, a_label = build_xfade_filter_complex([3.0, 4.0, 2.0])
    assert filt.count("xfade=") == 2
    assert filt.count("acrossfade=") == 2
    assert v_label == "vout"
    assert a_label == "aout"


def test_build_xfade_filter_complex_offsets_account_for_overlap():
    # 2번째 전환은 (1번째 클립 길이 + 2번째 클립 길이 - 전환시간) - 전환시간 지점에서 시작해야 한다
    filt, _, _ = build_xfade_filter_complex([3.0, 4.0, 2.0], transition_dur=0.5)
    assert "offset=2.500" in filt  # 3.0 - 0.5
    assert "offset=6.000" in filt  # (3.0+4.0-0.5) - 0.5


def test_build_xfade_filter_complex_two_clips():
    filt, v_label, a_label = build_xfade_filter_complex([5.0, 5.0])
    assert filt.count("xfade=") == 1
    assert v_label == "vout"


def test_compute_scene_windows_matches_xfade_offsets():
    # 캡션 재합성이 쓰는 절대 시간 구간이 실제 크로스페이드 전환 시점(offset)과 어긋나면
    # 자막이 화면 전환과 안 맞게 뜬다 — build_xfade_filter_complex의 offset과 반드시 같은
    # 숫자를 내야 한다(타이밍 드리프트가 이 기능의 최대 리스크).
    durations = [3.0, 4.0, 2.0]
    filt, _, _ = build_xfade_filter_complex(durations, transition_dur=0.5)
    windows = compute_scene_windows(durations, [1, 2, 3], transition_dur=0.5)

    assert windows[0] == {"seq": 1, "start_sec": 0.0, "end_sec": 3.0}
    assert f"offset={windows[1]['start_sec']:.3f}" in filt
    assert f"offset={windows[2]['start_sec']:.3f}" in filt
    assert windows[1]["end_sec"] == windows[2]["start_sec"] + 0.5  # 다음 전환 offset과 맞물림


def test_compute_scene_windows_single_scene():
    windows = compute_scene_windows([5.0], [7])
    assert windows == [{"seq": 7, "start_sec": 0.0, "end_sec": 5.0}]


def test_resolve_font_path_for_key_returns_registered_path():
    assert resolve_font_path_for_key("nanum_gothic") == "assets/fonts/NanumGothic.ttf"


def test_resolve_font_path_for_key_falls_back_when_unknown():
    # 레지스트리에 없는 키는 resolve_font_path()의 기존 폴백 체인으로 넘어가야 한다
    # (Railway 배포 안전장치가 깨지면 안 됨).
    assert resolve_font_path_for_key("존재하지-않는-폰트") == resolve_font_path()


def test_resolve_font_path_for_key_defaults_when_none():
    assert resolve_font_path_for_key(None) == resolve_font_path_for_key("nanum_gothic")


def _sample_scenes():
    return [
        {"seq": 1, "stage": "empathy", "caption": "고민되시죠"},
        {"seq": 2, "stage": "emotion", "caption": "답답하셨을 거예요"},
        {"seq": 3, "stage": "problem", "caption": "그냥 포기하게 되죠"},
        {"seq": 4, "stage": "solution", "caption": "이렇게 해결해요"},
        {"seq": 5, "stage": "result", "caption": "훨씬 편해졌어요"},
        {"seq": 6, "stage": "product", "caption": "본문에서 확인하세요"},
    ]


def test_resolve_scene_text_elements_first_scene_gets_hook_only():
    elements = resolve_scene_text_elements(_sample_scenes())
    assert elements[1]["texts"] == {"hook": "고민되시죠"}
    assert elements[1]["is_first"] is True


def test_resolve_scene_text_elements_pre_reveal_scene_has_no_sticky_cta():
    elements = resolve_scene_text_elements(_sample_scenes())
    assert elements[3]["texts"] == {"caption": "그냥 포기하게 되죠"}  # problem 단계, sticky_cta 없음


def test_resolve_scene_text_elements_post_reveal_middle_scene_gets_sticky_cta():
    elements = resolve_scene_text_elements(_sample_scenes())
    assert elements[4]["texts"] == {"caption": "이렇게 해결해요", "sticky_cta": "본문에서 확인하세요"}


def test_resolve_scene_text_elements_last_scene_has_no_sticky_cta():
    elements = resolve_scene_text_elements(_sample_scenes())
    assert elements[6]["texts"] == {"caption": "본문에서 확인하세요"}
    assert elements[6]["is_last"] is True


def test_resolve_scene_text_elements_custom_pre_reveal_stages_override_default():
    # 기획천재발견형처럼 pre-reveal 구간이 첫 단계뿐인 형식을 흉내낸다 — 기본값(표준 6단계)
    # 이었다면 sticky_cta가 없었을 problem 단계 씬도, 커스텀 pre_reveal_stages를 넘기면
    # 상품이 이미 등장한 것으로 취급돼 sticky_cta를 받는다.
    custom_pre_reveal = frozenset({"empathy"})
    elements = resolve_scene_text_elements(_sample_scenes(), custom_pre_reveal)
    assert elements[3]["texts"] == {"caption": "그냥 포기하게 되죠", "sticky_cta": "본문에서 확인하세요"}


def test_recomposite_captions_builds_one_enable_gated_block_per_scene(tmp_path, monkeypatch):
    # ffmpeg 실행 없이 filter_complex 문자열 구성만 검증한다(다른 build_* 테스트와 같은 관례).
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구 테스트", font, str(tmp_path), str(tmp_path / "final.mp4"),
    )

    assert len(calls) == 1
    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert filter_complex.count("enable=") >= len(scenes)  # 씬마다 최소 1개 + 고지문구
    assert "-c:a" in calls[0] and "copy" in calls[0]  # 오디오는 재인코딩 없이 복사


def test_recomposite_captions_applies_text_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])
    overrides = {"4": {"caption": {"text": "바뀐 자막", "x": 50, "y": 1400}}}

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구", font, str(tmp_path), str(tmp_path / "final.mp4"),
        overrides=overrides,
    )

    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert "x=50:" in filter_complex
    with open(os.path.join(str(tmp_path), "scene4.caption.line0.txt"), encoding="utf-8") as f:
        assert f.read() == "바뀐 자막"


def test_recomposite_captions_applies_font_size_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])
    overrides = {"4": {"caption": {"font_size": 90}}}

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구", font, str(tmp_path), str(tmp_path / "final.mp4"),
        overrides=overrides,
    )

    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert "fontsize=90" in filter_complex


def test_recomposite_captions_applies_text_color_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])
    overrides = {"4": {"caption": {"text_color": "#ff00ff"}}}

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구", font, str(tmp_path), str(tmp_path / "final.mp4"),
        overrides=overrides,
    )

    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert "fontcolor=#ff00ff" in filter_complex


def test_recomposite_captions_empty_override_text_suppresses_element(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])
    overrides = {"4": {"sticky_cta": {"text": ""}}}

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구", font, str(tmp_path), str(tmp_path / "final.mp4"),
        overrides=overrides,
    )

    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert "scene4.sticky_cta" not in filter_complex


def test_recomposite_captions_custom_pre_reveal_stages_changes_sticky_cta(tmp_path, monkeypatch):
    # scene3(problem)이 기본값으로는 pre-reveal이라 sticky_cta가 없지만, 커스텀
    # pre_reveal_stages={"empathy"}를 넘기면 이미 상품이 등장한 것으로 취급돼 생긴다.
    calls = []
    monkeypatch.setattr("app.media.render.run_ffmpeg", lambda args: calls.append(args))
    font = resolve_font_path()
    scenes = _sample_scenes()
    timeline = compute_scene_windows([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], [s["seq"] for s in scenes])

    recomposite_captions(
        "base.mp4", timeline, scenes, "고지문구", font, str(tmp_path), str(tmp_path / "final.mp4"),
        pre_reveal_stages=frozenset({"empathy"}),
    )

    filter_complex = calls[0][calls[0].index("-filter_complex") + 1]
    assert "scene3.sticky_cta" in filter_complex
