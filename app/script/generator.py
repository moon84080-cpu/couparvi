"""3호 직원 — 대본 생성.

analysis_json + product 정보 + tone 파라미터 + needs_education 플래그를 받아
docs/03_interfaces.md 4번 스키마의 script_json을 생성한다.
"""

from __future__ import annotations

import json
import time

import anthropic

from app.config import ANTHROPIC_API_KEY, DEFAULT_TARGET_PERSONA
from app.llm_utils import parse_json_response
from app.media.render import resolve_scene_duration_sec
from app.script.prompts import build_system_prompt, build_user_prompt

MODEL = "claude-sonnet-4-5"


def _normalize_scene_durations(script_json: dict) -> dict:
    """scenes[].duration_sec을 나레이션 길이에 맞춰 재설정한다(사용자 피드백, 2026-08-19).

    LLM이 "초당 5~6글자" 가이드(app/script/prompts.py)를 완벽히 따르지 않아 duration_sec이
    나레이션 길이와 크게 안 맞는 씬이 생기면, 렌더링에서 속도 clamp로 강제 조정되며 실제
    영상 길이가 설정값과 크게 어긋난다(app/media/render.py의 resolve_scene_duration_sec
    참고 — 라이브박스 2세대 대본 실측으로 최대 63% 차이 확인). 생성 직후 정규화해 화면에
    보이는 값과 실제 렌더 결과가 계속 일치하게 한다.
    """
    for scene in script_json.get("scenes") or []:
        scene["duration_sec"] = resolve_scene_duration_sec(scene.get("narration", ""), scene.get("duration_sec"))
    return script_json


class ScriptGenerationError(RuntimeError):
    """생성 실패(파싱 실패, API 오류 포함)를 감싸는 명확한 예외."""


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise ScriptGenerationError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_script(
    analysis_json: dict,
    product: dict,
    tone: str,
    needs_education: bool,
    target_persona: str = DEFAULT_TARGET_PERSONA,
    client: anthropic.Anthropic | None = None,
) -> dict:
    active_client = client or _client()
    system_prompt = build_system_prompt(tone, needs_education)
    user_prompt = build_user_prompt(analysis_json, product, target_persona)

    last_error: Exception | None = None
    for attempt in range(2):  # 최초 시도 + 재시도 1회 (AGENTS.md 코딩 컨벤션)
        try:
            message = active_client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = message.content[0].text
            return _normalize_scene_durations(parse_json_response(text))
        except (json.JSONDecodeError, anthropic.APIError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
                continue

    raise ScriptGenerationError(f"대본 생성 실패: {last_error}") from last_error
