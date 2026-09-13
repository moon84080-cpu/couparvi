# 03. Interfaces v2 — Phase 간 계약

## 1. DB 스키마

```sql
-- [1] 상품 발굴 + 스코어링
create table products (
  id uuid primary key default gen_random_uuid(),
  coupang_url text,
  keyword text,                         -- URL 대신 키워드 검색으로 발굴한 경우
  product_name text,
  price integer,
  discount_rate integer,
  image_urls jsonb default '[]',
  deeplink text,
  category text,
  review_count integer,
  price_score int check (price_score between 0 and 10),
  story_score int check (story_score between 0 and 10),              -- 공감 스토리 가능성 (리뷰 입력 후 채워짐)
  total_score int,                       -- review_count(20) + price_score(10) + story_score(10), 최대 40
  -- Phase 8에서 증가속도/충동구매/계절성/소재적합 4개 항목 삭제(migrations/008) — 실제
  -- 값을 채우는 경로가 없어 항상 0점이었다(app/discovery/scoring.py 참고).
  needs_education boolean not null default false,  -- 카테고리 키워드 매칭으로 자동 설정, 사람이 재검토/수정 가능
  status text not null default 'discovered',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- [2] 후기 수집 (수동 입력)
create table reviews (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references products(id) on delete cascade,
  reviews_raw text not null,            -- 수동 붙여넣은 원문 (분석 재료로만 사용, 절대 재배포 금지)
  -- Phase 9에서 rating_summary 컬럼 삭제(migrations/009) — 관련 UI/로직 전체 제거
  created_at timestamptz default now()
);

-- [3] AI 후기 분석
create table review_analysis (
  id uuid primary key default gen_random_uuid(),
  review_id uuid not null references reviews(id) on delete cascade,
  analysis_json jsonb not null,          -- 아래 "후기 분석 JSON 스키마"
  created_at timestamptz default now()
);

-- [4]+[5] 타겟 선정 + 대본 작성
create table scripts (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references products(id) on delete cascade,
  analysis_id uuid references review_analysis(id),
  target_persona text not null default '40-50대 여성',
  tone text not null,                    -- app/script/formats.py의 SCRIPT_FORMATS 키 중 하나(4번 참고)
  version integer not null default 1,
  script_json jsonb not null,            -- 아래 "대본 JSON 스키마"
  approved boolean not null default false,
  created_at timestamptz default now(),
  hook_preview_image_path text,          -- 후킹 확인 단계에서 만든 씬0 스틸컷 경로
  hook_preview_video_path text,          -- 후킹 확인 단계에서 만든 씬0 후킹 영상 경로
  hook_preview_status text,              -- null|generating|done|failed
  scene_preview_images jsonb not null default '{}'::jsonb
  -- 후킹(씬0) 외 다른 씬들의 미리보기: {"<seq>": {"image_path":.., "status":"generating"|"done"|"failed"}}
);

-- [6][7][8] 미디어
create table render_jobs (
  id uuid primary key default gen_random_uuid(),
  script_id uuid not null references scripts(id) on delete cascade,
  status text not null default 'queued', -- queued|generating_images|generating_video|generating_audio|assembling|done|failed
  output_path text,
  error_message text,
  created_at timestamptz default now(),
  finished_at timestamptz,
  base_video_path text,                  -- 자막 없는 합성본(크로스페이드+BGM까지 끝난 상태). 캡션 편집 재합성의 원본
  scene_timeline jsonb,                  -- base_video_path 기준 씬별 절대 구간: [{seq, start_sec, end_sec}]
  caption_overrides jsonb,               -- 씬별 캡션/후킹/상시CTA 위치·스타일 편집값. null이면 기본값. 아래 "4-1" 스키마
  kind text not null default 'full',     -- full|hook_preview|hook_patch (6번 참고)
  source_render_job_id uuid references render_jobs(id), -- hook_patch 전용: 어느 완료된 render_job을 고칠지
  target_seq int                         -- kind='hook_preview'|'hook_patch' 전용: 대상 씬 seq. null=후킹(scenes[0])
);

-- [9] 썸네일
create table thumbnails (
  id uuid primary key default gen_random_uuid(),
  render_job_id uuid not null references render_jobs(id) on delete cascade,
  image_path text,
  status text not null default 'queued', -- queued|done|failed
  created_at timestamptz default now()
);

-- [10] 업로드 준비 (냉각기간 + 수동 게시)
create table upload_queue (
  id uuid primary key default gen_random_uuid(),
  render_job_id uuid not null references render_jobs(id) on delete cascade,
  thumbnail_id uuid references thumbnails(id),
  youtube_title text,
  youtube_description text,
  ready_at timestamptz not null,         -- 냉각기간 종료 시각. 이 시각 이후에만 publish 가능(버튼 활성화)
  status text not null default 'pending_review', -- pending_review|ready_to_publish|published|canceled|failed
  youtube_video_id text,
  created_at timestamptz default now(),
  published_at timestamptz
);

-- 플랫폼 정책 모니터링 (Phase 4 부속 기능)
create table policy_snapshots (
  id uuid primary key default gen_random_uuid(),
  platform text not null,               -- youtube|instagram
  policy_name text not null,            -- 예: '광고 콘텐츠 정책', 'AI 생성 콘텐츠 정책'
  url text not null,
  content_hash text not null,           -- 마지막으로 확인한 페이지 본문의 해시
  last_checked_at timestamptz default now(),
  last_changed_at timestamptz
);

create table policy_alerts (
  id uuid primary key default gen_random_uuid(),
  snapshot_id uuid not null references policy_snapshots(id) on delete cascade,
  detected_at timestamptz default now(),
  reviewed boolean not null default false,
  reviewed_at timestamptz,
  note text                             -- 사람이 검토 후 남기는 메모(예: "실제 영향 없음" / "대본 규칙 수정 필요")
);
```

## 2. products.status 상태 머신

```
discovered → scored → reviews_collected → analyzed → script_generated → prompt_review →
script_approved → media_generated → thumbnail_generated → queued_for_upload → uploaded
(실패 시 각 단계 대응 failed_*, 재시도는 직전 단계로 롤백)

prompt_review: 대본 승인 직후 ~ "프롬프트 확인" 탭에서 씬별 스틸컷/후킹 영상을 확인·
확정하기 전까지. 확정(POST /prompts/{script_id}/confirm)해야 script_approved로 넘어가
실제 렌더가 큐잉되고 미디어제작 탭에 나타난다(Phase 7 — 확인 전에 미디어제작 탭에
노출돼 비용이 이중지출되는 문제를 막기 위해 분리).
```

## 3. 후기 분석 JSON 스키마 (review_analysis.analysis_json)

Claude가 reviews_raw를 입력받아 생성. **원문을 그대로 옮기지 않고 카테고리화**한다.

```json
{
  "positives": ["부드러운 원단", "한 장씩 잘 뽑힘"],
  "complaints": ["엠보싱이 얇게 느껴질 수 있음"],
  "surprises": ["가성비 대비 만족도가 높다는 반응"],
  "repurchase_reasons": ["대용량이라 쟁여두기 좋음"],
  "emotional_keywords": ["안심", "든든함", "데일리"],
  "target_segments": ["아이 키우는 집", "생필품 재구매층"],
  "suggested_hook_angle": "믿고 쓰는 브랜드로 갈아탄 이유"
}
```

## 4. 대본 JSON 스키마 (scripts.script_json)

```json
{
  "structure": {
    "empathy": "공감 문장 (권장 약 3초, 타겟이 '어 이거 내 얘기네' 하는 지점)",
    "emotion": "공감을 감정으로 증폭시키는 문장 (권장 약 4초, 답답함/불안/설렘 등 구체적 감정)",
    "problem": "그 감정의 원인이 되는 문제를 명확히 짚는 문장 (권장 약 6초)",
    "solution": "문제를 해결하는 방향성을 제시하는 문장 (권장 약 8초, 아직 상품명 등장 전)",
    "result": "그 방향대로 했을 때의 실제 효과·변화 (권장 약 5초, 리뷰의 긍정 후기를 재구성)",
    "product": "상품 등장 + CTA까지 포함하는 마무리 문장들 (권장 약 6초, 가격은 언급하지 않는다)"
  },
  "educational_note": {
    "included": true,
    "text": "자외선A는 피부 노화를, 자외선B는 화상과 색소침착을 유발할 수 있어요. 흐린 날에도 자외선은 그대로 통과합니다."
  },
  "tone": "생활팁",
  "scenes": [
    {
      "seq": 1,
      "stage": "empathy",
      "situation": "situation_hook",
      "narration": "...",
      "caption": "...",
      "visual": "감정/분위기 태그: 구체적으로 어떤 장면이 보이면 좋을지",
      "image_index": 0,
      "duration_sec": 5
    }
  ],
  "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
  "estimated_duration_sec": 45,
  "youtube": {
    "title": "...",
    "description": "... (마지막 줄은 반드시 disclosure와 정확히 동일한 문구)",
    "tags": ["..."]
  }
}
```

**v2.4부터 구조는 형식(`tone`)마다 다르다.** 위 예시는 표준 6단계(공감 → 감정 → 문제제기 →
해결 → 결과 → 상품) 형식이며, `tone` 값이 이 형식이 아닌 다른 형식이면 `structure`의 필드
구성/개수, `scenes[].stage`로 쓸 수 있는 값, CTA가 들어가는 필드명이 달라진다. 실제 정의는
`app/script/formats.py`의 `SCRIPT_FORMATS` 레지스트리가 유일한 소스다 — 이 문서에 형식마다
전체 구조를 다시 나열하지 않는다(형식이 늘어날 때마다 문서가 낡는 것을 방지). 요약:
- `structure`의 필드는 `SCRIPT_FORMATS[tone].stages`가 정의한 키와 정확히 일치해야 하며 모두
  비어있지 않아야 한다.
- `scenes[].stage`는 그 형식의 stage 키 목록 중 하나여야 한다(대본작성 탭에서 구조 카드마다
  해당 자막을 같이 보여주는 데 쓰인다). `scenes`는 형식이 정의한 stage 순서대로 정렬한다.
- CTA(가격 언급 없이 "본문/더보기/설명란" 등으로 유도하는 문구)는 별도 단계가 아니라 그
  형식의 `cta_stage` 필드 마지막 문장에 자연스럽게 포함한다.
- `tone`은 `SCRIPT_FORMATS`에 정의된 값 중 하나. 신규 대본 생성(대시보드/`/api/products/{id}/
  generate-script`)은 `active_tone_choices()`가 반환하는 신규 형식만 선택지로 제공한다.
  v2.4 이전 생성분의 구 7종 톤(불편해결/우월감/보상/생활팁/사실형/생활형/실리적)은
  `legacy=True`로 레지스트리에 남아 있어 계속 렌더링/편집 가능하지만 신규 생성 선택지에는
  노출되지 않는다.
- `scenes[].visual`은 그 씬에 어울리는 화면 연출을 짧게 적어 후보 이미지 중 실제로 쓰일 컷을
  고르는 데 참고한다(`app/script/image_matcher.py`) — 형식과 무관하게 공통.
- `scenes[].situation`(v2.5)은 `stage`와 별개로, 형식(`tone`)과 무관하게 항상
  `app/script/formats.py`의 `SITUATION_CHOICES` 5개 값(`situation_hook` / `situation_analysis`
  / `situation_usage` / `situation_conclusion` / `situation_cta`) 중 하나다. 이미지 생성
  (`app/media/image_generator.py`의 `SITUATION_STYLE_PRESETS`)이 이 값으로 씬마다 다른 화면
  스타일(정보 전달형/시연형/마무리형 등)을 얹는다 — 형식이 7종으로 늘어나도 프리셋 매핑표는
  그대로 5개만 유지된다.

**고지문구(disclosure) 배치(v2.2)**: `PARTNERS_DISCLOSURE` 문구는 narration(음성으로 낭독되는 모든 텍스트: `structure.*`, `scenes[].narration`)에는 절대 포함하지 않는다. `youtube.description`의 맨 마지막 줄에만 정확히 그대로 넣는다. 영상 내 마지막 장면 시각적 오버레이(Phase 3, `disclosure` 필드 사용)는 이와 별개로 항상 표시되므로 음성으로 다시 읽을 필요가 없다.

**`educational_note` (선택적, 카테고리 기반)**: `products.needs_education=true`인 상품만 채운다. 형식이 정의한 특정 단계(`SCRIPT_FORMATS[tone].educational_note_after_stage` — 표준 6단계 형식은 `problem`)를 보강하는 1~2문장짜리 간단한 사실 설명이며, 별도 구조 단계가 아니라 그 단계 씬 직후에 삽입되는 부가 정보다. 자외선차단·살균·정수필터·유산균 등 "왜 필요한지"에 대한 배경지식이 설득력을 좌우하는 카테고리에 쓴다. 규칙:
- 일반적으로 널리 합의된 상식 수준의 사실만 사용 (예: "자외선은 피부 노화·화상을 유발할 수 있다"는 O, 특정 질병 진단·치료 효과를 암시하는 표현은 X)
- 의학적 조언·진단처럼 들리는 표현 금지 ("~하면 안 걸립니다", "치료됩니다" 등)
- 과장·확정적 인과 표현 대신 "~일 수 있어요", "~로 알려져 있어요" 같은 절제된 어투 사용
- `needs_education=false`인 상품은 `included: false, text: ""`로 둔다

제약: `scenes` 3~8개, 30~45초(형식과 무관하게 전역), `narration`은 reviews_raw와 8단어 이상 연속 일치 금지, `structure`는 형식이 정의한 필드 전부 필수(비어있지 않음), `scenes[].stage`는 그 형식의 stage 키 중 하나여야 함, `scenes[].situation`은 `SITUATION_CHOICES` 5개 값 중 하나여야 함(형식과 무관), `tone`은 `SCRIPT_FORMATS`에 정의된 값 중 하나.

## 4-1. 캡션 오버라이드 JSON 스키마 (render_jobs.caption_overrides)

미디어제작 탭의 캡션 편집기(씬별 자막/후킹/상시CTA 위치·스타일 편집, CapCut식 드래그
에디터)가 저장하는 값이다. `scripts.script_json`은 건드리지 않는다 — 대본 내용과 "이 렌더를
어떻게 보여줄지"는 별개 개념이기 때문.

```json
{
  "2": {
    "caption": {
      "text": "이 필터 하나로 끝",
      "x": 96, "y": 1500,
      "font": "nanum_gothic",
      "font_size": 60,
      "bg_enabled": true,
      "bg_color": "#000000",
      "bg_opacity": 0.55
    },
    "sticky_cta": {
      "text": "본문에서 확인",
      "x": 200, "y": 1690,
      "font": "nanum_gothic",
      "font_size": 32,
      "bg_enabled": false,
      "bg_color": "#000000",
      "bg_opacity": 0.0
    }
  }
}
```

- 최상위 키는 `scenes[].seq`를 문자열로 표현한 것(JSON 객체 키는 항상 문자열). 그 아래 키는
  `caption`(씬 하단 일반 자막) / `hook`(첫 씬 상단 후킹 헤드라인) / `sticky_cta`(중간
  씬들의 상시 노출 CTA 배너) 중 편집기가 채운 것만 존재 — **고지문구(disclosure)는 법적
  요건(`PARTNERS_DISCLOSURE`, 절대 규칙 3번)이라 편집 대상이 아니고 이 키에 절대 들어가지
  않는다.**
- `x`/`y`는 1080x1920 캔버스 기준 절대 픽셀 좌표(텍스트 블록 좌상단 기준). 씬/타입 조합이
  이 객체에 없으면 그 요소는 `app/media/render.py`의 기존 기본 위치(하단 마진 + 가운데
  정렬 등)로 렌더된다 — 하위호환을 위한 기본값이며, `caption_overrides`가 아예 `null`인
  렌더(이 기능 이전에 만들어진 것 포함)는 지금과 완전히 동일하게 동작한다.
- `text`가 빈 문자열이면 그 요소를 화면에서 아예 숨긴다(기존 `caption_text=""` 처리와 동일).
- `font`는 `app/config.py`의 `FONT_REGISTRY` 키 중 하나만 허용한다.
- `font_size`는 20~120px 범위의 정수만 허용(그 밖의 값/파싱 실패는 저장 시 제거되고 기본값으로
  대체). 씬/타입 조합에 없으면 요소별 기존 기본 크기(일반 자막 60px, 마지막씬 CTA 자막 46px,
  후킹 헤드라인 66px, 상시CTA 배너 32px)로 렌더된다 — 줄바꿈된 문구의 줄간격도 크기 비율에
  맞춰 같이 조정된다.

## 5. API 엔드포인트

| Method | Path | 설명 | Phase |
|---|---|---|---|
| POST | `/api/products/discover` | body: `{coupang_url}` 또는 `{keyword}`. 쿠팡 파트너스 API로 상품정보+점수 계산 → `scored` | 1 |
| POST | `/api/products/manual` | (v2.2, 파트너스 API 키 미발급 시 임시 경로) body: `{product_name, price?, category?, image_urls?, coupang_url?, review_count?}`. API 호출 없이 사람이 직접 입력한 값으로 상품 등록+점수 계산 → `scored`. `deeplink`는 비워둔 채 생성되며 이후 `/deeplink`로 채운다 | 1 |
| PATCH | `/api/products/{id}/deeplink` | (v2.2) body: `{deeplink}`. 쿠팡 파트너스 웹사이트에서 사람이 수동 생성한 딥링크를 등록/수정 | 1 |
| GET | `/api/products?min_score=70` | 추천 후보 목록 | 1 |
| POST | `/api/products/{id}/reviews` | 리뷰 원문+별점 저장 → `reviews_collected` | 1 |
| POST | `/api/products/{id}/analyze-reviews` | Claude 분석 → `analyzed` | 2 |
| POST | `/api/products/{id}/generate-script` | body: `{tone, target_persona?}` → `script_generated` | 2 |
| PATCH | `/api/scripts/{id}` | 수동 수정, version+1 | 2 |
| POST | `/api/scripts/{id}/approve` | → `script_approved`, render_jobs enqueue | 3 |
| GET | `/api/render-jobs/{id}` | 렌더 상태 | 3 |
| POST | `/api/render-jobs/{id}/generate-thumbnail` | 썸네일 생성 | 4 |
| POST | `/api/render-jobs/{id}/queue-upload` | body: `{cooldown_minutes?}`(기본 240) → upload_queue 생성(`pending_review`), `queued_for_upload` | 4 |
| POST | `/api/upload-queue/{id}/cancel` | 언제든 취소 가능 → `canceled` | 4 |
| POST | `/api/upload-queue/{id}/publish` | **사람이 명시적으로 호출.** `ready_to_publish` 상태일 때만 허용, 이때 실제 유튜브 업로드 API가 호출된다 → `published` | 4 |
| GET | `/api/upload-queue?status=` | 검토/게시 대기 목록 (대시보드용) | 4 |
| POST | `/api/policy-check/run` | cron-job.org 등 외부 스케줄러가 주기 호출. 등록된 정책 페이지를 fetch해 해시 비교, 변경 시 policy_alerts 생성 | 4 |
| GET | `/api/policy-alerts?reviewed=false` | 미검토 정책 변경 알림 목록 (대시보드 배너용) | 4 |
| POST | `/api/policy-alerts/{id}/review` | body: `{note}` → reviewed=true 처리 | 4 |

내부 상태 워커: `pending_review` 항목 중 `ready_at` 경과분을 `ready_to_publish`로 전환하는 것만 수행 — **이 워커는 외부(유튜브) API를 호출하지 않는다.** 실제 유튜브 업로드는 오직 `/publish` 엔드포인트를 사람이 호출했을 때만 발생한다. Phase 4에서 구현.

## 6. 환경변수

| 변수 | 용도 |
|---|---|
| `COUPANG_ACCESS_KEY` / `COUPANG_SECRET_KEY` | 파트너스 API |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | DB/Storage |
| `ANTHROPIC_API_KEY` | 후기 분석, 대본 생성 |
| `TTS_PROVIDER` | (v2.2) `elevenlabs` \| `edge`. 기본값 `edge` — ElevenLabs 무료 플랜은 라이브러리 보이스를 API로 못 씀(2026-07-13 확인, `01_plan.md` 참조). edge-tts는 키 불필요 |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | TTS (`TTS_PROVIDER=elevenlabs`일 때만 사용) |
| `EDGE_TTS_VOICE` | TTS (`TTS_PROVIDER=edge`일 때 보이스 이름, 기본값 `ko-KR-SunHiNeural`) |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` | 업로드 (Phase 4) |
| `DEFAULT_HOLD_MINUTES` | 게시 전 냉각기간 기본값(분). 경과해야 `/publish` 버튼이 활성화됨 (기본 240) |
| `STORAGE_BUCKET` | 완성 영상/썸네일 버킷 |
| `POLICY_CHECK_SECRET` | `/api/policy-check/run` 호출 인증용(외부 스케줄러가 헤더로 전달, 무단 트리거 방지) |
| `SESSION_SECRET` | 로그인 세션 쿠키 서명용. 단일 운영자 계정은 Supabase Auth에서 수동 생성(회원가입 화면 없음) |

## 8. 사실설명(educational_note) 트리거 키워드

`app/config.py`의 `EDUCATION_TRIGGER_KEYWORDS` 상수. 상품 발굴 시 `category`+`product_name`에 아래 키워드가 포함되면 `needs_education=true`로 자동 설정한다(사람이 대시보드에서 재검토·수정 가능).

```python
EDUCATION_TRIGGER_KEYWORDS = [
    "자외선", "UV", "차단제", "선크림",
    "살균", "항균", "소독",
    "정수", "필터", "공기청정",
    "유산균", "프로바이오틱스",
    "탈취", "제습",
]
```

목록은 초기값이며 운영하면서 대시보드에서 상품별로 수동 토글한 이력을 모아 주기적으로 보강한다.

## 9. 정책 모니터링 대상 (policy_snapshots 초기 시드)

`app/config.py`의 `MONITORED_POLICIES` 상수 — 최초 배포 시 이 목록으로 policy_snapshots를 시드한다.

```python
MONITORED_POLICIES = [
    {"platform": "youtube", "policy_name": "커뮤니티 가이드라인", "url": "https://www.youtube.com/howyoutubeworks/policies/community-guidelines/"},
    {"platform": "youtube", "policy_name": "광고 콘텐츠 정책", "url": "https://support.google.com/youtube/answer/154235"},
    {"platform": "youtube", "policy_name": "변경되거나 합성된 콘텐츠 정책", "url": "https://support.google.com/youtube/answer/14328491"},
    {"platform": "instagram", "policy_name": "커뮤니티 가이드라인", "url": "https://help.instagram.com/477434105621119"},
    {"platform": "instagram", "policy_name": "브랜드 콘텐츠 정책", "url": "https://help.instagram.com/2028067040742191"},
]
```

정확한 URL은 Phase 4 착수 시 최신 링크로 재확인 후 반영한다(정책 페이지 경로는 종종 바뀐다). cron-job.org에서 주 1회 `POST /api/policy-check/run`을 호출하도록 설정한다. 해시 비교는 페이지 전체 텍스트 기준이라 쿠키 배너 등 무관한 변경도 오탐지될 수 있음을 감안하고, 알림은 "확인이 필요하다"는 신호로만 쓴다 — 실제 정책 위반 여부 판단은 사람이 한다.

## 10. 프로젝트 구조 (목표)

```
app/
├── main.py
├── config.py               # PARTNERS_DISCLOSURE, DEFAULT_HOLD_MINUTES 등
├── db.py
├── coupang/partners.py     # HMAC, 상품 검색, 딥링크
├── discovery/scoring.py    # 1호 직원: 스코어링 로직 (Phase 1)
├── review/analyzer.py      # 2호 직원: 후기 분석 (Phase 2)
├── script/generator.py     # 3호 직원: 구조+톤 대본 생성 (Phase 2)
├── script/validator.py     # 원문유출 검사, 스키마 검증 (Phase 2)
├── media/images.py         # 4호 직원: 실사 상품 이미지 가공 (Phase 3)
├── media/graphics.py       # 4호 직원: 2D 그래픽 배경/카드/아이콘 레이어 (Phase 3, C안)
├── media/tts.py            # 4호 직원: TTS (Phase 3)
├── media/render.py         # 5호 직원: FFmpeg 조립 (Phase 3)
├── media/thumbnail.py      # 5호 직원: 썸네일 (Phase 4)
├── upload/queue_worker.py  # 냉각기간 경과 시 상태만 전환 (외부 API 호출 없음, Phase 4)
├── upload/publisher.py     # /publish 호출 시에만 실제 유튜브 업로드 실행 (Phase 4)
├── monitoring/policy_checker.py  # 정책 페이지 해시 비교, 변경 감지 (Phase 4)
└── web/                    # 대시보드 (Phase 4)
```
