# CLAUDE.md — agape (SNS 헤어 트렌드 감지 파이프라인)

네이버 데이터랩 + YouTube Data API + Pinterest(KR 크롤링/공식 API)로 한국 헤어 키워드의
급등 시그널을 주간 감지하는 저비용(월 $0~10) 배치 파이프라인. 웹서버 없음 — 수집(JSONL) →
DuckDB 집계 → 리포트(Slack/이메일/GitHub Pages)가 전부다.

## 환경 · 실행

- **독립 레포** — 원래 회사 모노레포(AcrossB/pollux)의 `pp/` 하위 디렉토리에서 시작해
  `agape`로 개명했고,
  개인 계정 레포로 분리됐다. 의존성은 httpx/duckdb/pyyaml/dotenv 4개뿐.
  표준 라이브러리로 충분하면 의존성을 늘리지 않는다(이메일은 smtplib).
- **반드시 프로젝트 루트(pyproject.toml 위치)에서 `uv run agape ...`로 실행**. 다른 디렉토리에서
  실행하면 uv가 프로젝트를 못 찾고 macOS 시스템 `agape` 바이너리(ASN.1 도구)가 잡힌다 —
  과거에 실제로 겪은 함정.
- 레이아웃: `src/` 평탄 구조를 import명 `agape`로 매핑 (setuptools `package-dir`).
  **hatchling으로 되돌리지 말 것** — prefix 변경 매핑은 hatchling editable 설치가 지원 안 함.
- 명령: `collect`(매일 수집) / `discover`(주 1회, 유튜브 영상 풀) /
  `report [--send --only --html --quiet]` / `query`(애드혹 조회, 저장 안 함).

## 아키텍처 불변식 (깨면 데이터가 조용히 오염된다)

1. **네이버 ratio는 요청 내 상대값** (기간·그룹 내 최대=100). 배치 간 비교를 위해 모든 요청에
   앵커 키워드(`keywords.yaml`의 `anchor`)를 끼워 앵커 평균으로 나눈 `ratio_adj`를 저장한다.
   앵커를 빼거나 배치 구성을 바꾸는 수정은 스케일을 깨뜨린다.
2. **필터 태그 격리**: 네이버 수집 행에는 `filters` 태그(예: `all`, `mo|f|20,30`)가 붙고,
   집계는 현재 설정과 태그가 일치하는 행만 읽는다. 필터 변경 시 자동 재백필(상태:
   `naver_filters_tag`). 태그 없이 행을 쓰거나 태그 무시 집계를 추가하지 말 것.
3. **저장은 멱등**: (keyword, date) 중복은 `fetched_at` 최신본으로 dedupe. 스키마가 변한
   과거 파일과의 호환을 위해 DuckDB는 `read_ndjson_auto(..., union_by_name=true)`로 읽는다.
4. **수집기는 소스별 독립 실행** (`cli.cmd_collect`): 키 부재(SystemExit)는 SKIP으로 exit 0
   (선택 소스가 있으므로 — CI에서 미등록 시크릿이 정상 케이스), 예기치 못한 예외는 ERROR로
   exit 1, 전부 스킵이면 exit 1. 새 수집기도 이 루프에 넣고, 키 검증은 SystemExit으로 던진다.
5. **유튜브 쿼터 경제학**: `search.list`=100유닛(비쌈)이라 discover에서 주 1회 키워드 순환만.
   시계열은 `videos.list`(1유닛/50개) 일일 스냅샷의 차분(Δviews)으로 직접 만든다 —
   유튜브는 과거 조회수를 주지 않는다.
6. **크롤링은 robots 가드 필수**: 모든 크롤링 요청 전 `robots.RobotsGuard.ensure_allowed(url)`.
   RFC 9309 — 4xx=제한 없음, 5xx/네트워크 오류=전면 차단(fail-closed). robots 판정과 실제
   요청에 같은 UA를 쓴다. 공식 API 호출(네이버/유튜브/핀터레스트 공식)은 대상이 아니다.
   새 크롤링 소스(올리브영 등)를 추가하면 반드시 이 가드를 붙일 것.
7. **Pinterest 이원화**: KR 데이터 = trends.pinterest.com 비공식 API 크롤링(언제든 깨질 수
   있음 — 실패 허용). US/글로벌 = 공식 API(client_credentials, 파트너 미승인 시 403 → 스킵).
   **공식 API는 KR을 지원하지 않는다** — KR을 공식 API로 옮기려는 시도는 불가능하다.
8. **성장률 단위 주의**: 핀터레스트 비공식 `/metrics/`·`/top_trends_filtered/`는 소수 비율
   (0.4=+40%), 공식 API `percent_growth_mom`은 이미 퍼센트(85=+85%). 포맷터에서 ×100을
   섞으면 안 된다 (report.py에 케이스별 처리 있음).

## 시그널 판정 (metrics.py 상수)

주간 볼륨 기준: z-score(4주 이동평균 대비) ≥ 2 = 급등, WoW ≥ +15% = 상승,
YoY < +50% = 계절성 의심(작년에도 이맘때 높았음), 네이버 급등 + (유튜브 or 핀터레스트)
동반 상승 = "강한 후보". 임계값 조정은 `metrics.py` 상단 상수에서만.

## 설정 · 비밀정보

- **비밀은 `.env`** (gitignore): NAVER_*, YOUTUBE_API_KEY, PINTEREST_*, SMTP_*, SLACK_WEBHOOK_URL.
  `load_dotenv`는 기존 환경변수를 덮지 않으므로 CI에서는 시크릿을 env로 주입한다.
- **대상/정책은 YAML**: `keywords.yaml`(키워드·앵커·필터 — 추적 대상의 단일 진실),
  `channels.yaml`(전송 채널, gitignore — 템플릿은 `channels.example.yaml`, CI에서는 레포 변수
  `CHANNELS_YAML`로 생성).
- 시크릿 값을 채팅/코드/커밋에 넣지 않는다.

## Git · 계정 (멀티 계정 환경)

- 이 레포는 **개인 GitHub 계정** 소속이다. 이 머신의 전역 git identity는 회사 계정
  (`bang@acrossb.net`, AcrossB는 HTTPS+키체인 인증)이므로 섞이지 않게 분리돼 있다:
  - remote는 SSH 별칭 사용: `git@github-personal:<개인계정>/<레포>.git`
    (`~/.ssh/config`의 `Host github-personal` → 개인 전용 키 `id_ed25519_personal`)
  - 커밋 identity는 **per-repo `user.name`/`user.email`(개인)** — 전역(회사) 설정이
    이 레포에 쓰이면 안 된다. remote를 `github.com`으로 바꾸면 개인 키 라우팅이 깨진다.
- 브랜치 생성/전환·커밋·푸시는 사용자가 직접 관리한다 — 요청 없이 실행하지 말 것.

## CI (GitHub Actions)

`.github/workflows/agape.yml` (레포 루트 `.github/`). 매일 collect, 월요일 discover+report+Pages.
`data/`는 실행 후 `git add -f`로 커밋백(로컬 gitignore를 CI에서만 우회하는 의도적 설계이며,
커밋 주체는 github-actions 봇이라 개인 identity와 무관). 시크릿은 레포 Actions Secrets,
전송 채널은 레포 변수 `CHANNELS_YAML`로 주입. 공개↔비공개 전환: 레포 가시성 변경 +
레포 변수 `PUBLISH_PAGES=false`(Pages만 끔, 수집·전송은 유지).

## 테스트 관례

전용 테스트 프레임워크 없음 — 합성 JSONL을 `data/raw/`에 쓰고 지표를 검증한 뒤 지우는
스모크 스크립트 방식(`uv run python - <<EOF`). 외부 전송(Slack/SMTP)과 HTTP는 반드시
mock/patch로 격리해 실제 발송이 나가지 않게 한다. 수정 후 최소 확인:
`uv run agape report`(기존 데이터로 렌더링) + 관련 합성 데이터 케이스.
