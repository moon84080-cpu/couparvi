import os

from dotenv import load_dotenv

load_dotenv()

# AGENTS.md 절대 규칙 3 — 모든 대본/유튜브 설명문에 하드코딩으로 포함해야 하는 고지문구.
PARTNERS_DISCLOSURE = (
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
)

# 게시 전 냉각기간 기본값(분). 경과해야 /publish 버튼이 활성화된다 (Phase 4).
DEFAULT_HOLD_MINUTES = int(os.getenv("DEFAULT_HOLD_MINUTES", "240"))

# docs/03_interfaces.md 8번 — 상품 발굴 시 category+product_name에 포함되면 needs_education=true 자동 설정.
# 목록은 초기값이며 운영하면서 보강한다.
EDUCATION_TRIGGER_KEYWORDS = [
    "자외선", "UV", "차단제", "선크림",
    "살균", "항균", "소독",
    "정수", "필터", "공기청정",
    "유산균", "프로바이오틱스",
    "탈취", "제습",
]

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY", "")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY", "")

# 네이버 쇼핑검색 오픈API — 쿠팡과 무관, 수동 등록 상품의 대표 이미지 자동 채움용
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
# 네이버 쇼핑검색 API 자체를 켤지 여부. 2026-08-17 네이버 쪽 정책 변경으로 이 앱에 등록된
# 키의 쇼핑검색 API가 막혀서(SE05: 존재하지 않는 검색 api입니다 — 같은 키의 블로그검색은
# 정상 동작해 키 자체 문제는 아님을 확인함) 한시적으로 기능 자체를 끈다(사용자 피드백).
# 나중에 네이버 쪽 정책이 정리되면 이 값을 true로 되돌리면 코드 변경 없이 재활성화된다.
NAVER_SEARCH_ENABLED = os.getenv("NAVER_SEARCH_ENABLED", "false").lower() == "true"

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# 리뷰 스크린샷 OCR(app/review/ocr.py) 공급자 — TTS_PROVIDER와 같은 패턴.
# "gemini"(기본값, gemini-3.5-flash-lite): 실제 리뷰 캡처 2장으로 claude와 비교 실측한
# 결과(사용자 피드백, 2026-08-18) 속도(4~12배)·정확도·비용 모두 압도적으로 우세해
# 기본값으로 승격했다 — 특히 글자가 작고 빽빽한 캡처에서 claude는 문장이 통째로
# 깨졌지만 gemini는 원문과 거의 동일하게 추출했다. 이미 있는 GEMINI_API_KEY로 바로
# 동작한다. 다른 선택지: "claude"(예전 기본값, ANTHROPIC_API_KEY), "openai"(gpt-4o-mini,
# OPENAI_API_KEY — 추가만 하고 인식률은 아직 검증 안 함).
OCR_PROVIDER = os.getenv("OCR_PROVIDER", "gemini")

# Gemini(나노바나나) 이미지 생성 — 씬의 화면 연출에 맞춰 실사 상품 사진을 참고 이미지로
# 넣어 새 이미지를 만드는 데 쓴다 (app/media/image_generator.py).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# TTS 공급자 선택 — "elevenlabs" | "edge". ElevenLabs 무료 플랜은 라이브러리 보이스를
# API로 못 쓰는 제약이 있어(docs/01_plan.md 2026-07-13 이력 참조), 기본값은 키 없이도
# 바로 쓸 수 있는 edge-tts(Microsoft Edge 읽어주기 기능의 비공식 클라이언트)로 둔다.
# edge-tts는 공식 API가 아니므로 Suno와 동일한 리스크(공지 없는 차단 가능성)가 있다는
# 점을 인지하고 쓴다 — 다만 TTS는 쿠팡 계정 정지 같은 치명적 리스크와는 무관하다.
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge")
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "ko-KR-SunHiNeural")

# GET /api/products 기본 노출 임계값. 예전엔 100점 만점에 80점이었는데, 스코어링을
# 리뷰수/가격/스토리 3항목(최대 40점)만 남기고 정리하면서(사용자 피드백 — 나머지 4항목은
# 실제로 값이 채워지는 경로가 없어 항상 0점이었다) 같은 선별 강도(80%)를 유지하도록
# 비율 그대로 낮췄다.
DEFAULT_MIN_SCORE = 32

DEFAULT_TARGET_PERSONA = "40-50대 여성"

# docs/03_interfaces.md 4번 — educational_note에 절대 포함되면 안 되는 진단/치료/효능보장 암시 표현.
# 목록은 초기값이며 운영하면서 보강한다.
EDUCATION_FORBIDDEN_KEYWORDS = [
    "치료됩니다", "완치", "100% 예방", "완치됩니다", "예방됩니다",
    "질병을 낫게", "효과가 보장", "부작용 없이 치료", "즉시 완쾌",
]

# --- Phase 4 ---

YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "")

# /api/policy-check/run 호출 인증용 — 외부 스케줄러(cron-job.org)가 헤더로 전달, 무단 트리거 방지.
POLICY_CHECK_SECRET = os.getenv("POLICY_CHECK_SECRET", "")

# 로그인 세션 쿠키 서명용.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_COOKIE_NAME = "couparvi_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7일

# docs/03_interfaces.md 4-1번 — 캡션 편집기(씬별 자막/후킹/상시CTA)의 폰트 선택지.
# 전부 SIL OFL 라이선스로 google/fonts 저장소에서 받아 assets/fonts/에 라이선스 파일과
# 함께 번들했다(나눔고딕과 같은 방식). 키는 caption_overrides JSON의 "font" 값과 1:1 대응.
FONT_REGISTRY = {
    "nanum_gothic": "assets/fonts/NanumGothic.ttf",
    "black_han_sans": "assets/fonts/BlackHanSans.ttf",
    "gothic_a1": "assets/fonts/GothicA1-Bold.ttf",
}
DEFAULT_FONT_KEY = "nanum_gothic"
FONT_LABELS = {
    "nanum_gothic": "나눔고딕 (기본)",
    "black_han_sans": "블랙한산스 (임팩트)",
    "gothic_a1": "고딕 A1 (깔끔)",
}

# docs/03_interfaces.md 9번 — policy_snapshots 초기 시드 목록. 정확한 URL은 Phase 4
# 착수 시 최신 링크로 재확인 후 반영한다(정책 페이지 경로는 종종 바뀐다).
MONITORED_POLICIES = [
    {"platform": "youtube", "policy_name": "커뮤니티 가이드라인", "url": "https://www.youtube.com/howyoutubeworks/policies/community-guidelines/"},
    {"platform": "youtube", "policy_name": "광고 콘텐츠 정책", "url": "https://support.google.com/youtube/answer/154235"},
    {"platform": "youtube", "policy_name": "변경되거나 합성된 콘텐츠 정책", "url": "https://support.google.com/youtube/answer/14328491"},
    {"platform": "instagram", "policy_name": "커뮤니티 가이드라인", "url": "https://help.instagram.com/477434105621119"},
    {"platform": "instagram", "policy_name": "브랜드 콘텐츠 정책", "url": "https://help.instagram.com/2028067040742191"},
]
