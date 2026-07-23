from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.environ.get("AGAPE_DATA_DIR") or str(PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
STATE_DIR = DATA_DIR / "state"

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# 이메일 발송용 SMTP (비밀정보 — .env). 수신자는 channels.yaml에서 지정한다.
# CI에서는 미등록 시크릿이 빈 문자열로 주입되므로 "키 없음"과 "빈 값"을 동일하게 취급한다.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
SMTP_STARTTLS = (os.environ.get("SMTP_STARTTLS") or "true").lower() != "false"

# 네이버 데이터랩: 요청당 키워드 그룹 최대 5개 → 앵커 1 + 추적 키워드 4
NAVER_GROUPS_PER_REQUEST = 4
# 최초 실행 시 백필 기간(일). 데이터랩은 2016-01-01부터 과거 데이터를 제공한다.
NAVER_BACKFILL_DAYS = 730
# 평시 수집 시 재조회 기간(일). 늦게 집계되는 데이터를 덮어쓰기 위한 여유분.
NAVER_INCREMENTAL_DAYS = 30

# 크롤링 수집기 공통: 정직한 봇 UA — robots.txt 판정과 실제 요청에 동일하게 사용
CRAWLER_USER_AGENT = "agape-trendbot/0.1 (personal trend research)"

# Pinterest Trends KR (비공식 내부 API — 로그아웃 공개 데이터, 주간 집계)
PINTEREST_COUNTRY = "KR"
PINTEREST_METRICS_BATCH = 15  # /metrics/ 요청당 키워드 수 (웹 UI 자체가 ~20개씩 요청)
PINTEREST_TOP_N = 50
PINTEREST_LOOKBACK_DAYS = 90

# Pinterest KR 크롤러 인증 (선택) — 없어도 동작한다. 차단 완화나 세션이 필요할 때만 설정.
# robots 판정과 실제 요청에 동일한 UA를 쓰기 위해 CRAWLER_USER_AGENT를 덮어쓴다.
PINTEREST_COOKIE = os.environ.get("PINTEREST_COOKIE", "")
PINTEREST_CRAWLER_UA = os.environ.get("PINTEREST_CRAWLER_UA", "").strip() or CRAWLER_USER_AGENT

# Pinterest 공식 Trends API (선택) — US/글로벌 선행 신호용. 한국(KR)은 공식 API 미지원.
# client_credentials 방식이라 OAuth 리다이렉트가 필요 없다(앱 ID/Secret만).
# 단, Trends 데이터 접근은 Pinterest 파트너/승인이 필요할 수 있어 미승인 시 403이 난다.
PINTEREST_APP_ID = os.environ.get("PINTEREST_APP_ID", "")
PINTEREST_APP_SECRET = os.environ.get("PINTEREST_APP_SECRET", "")
PINTEREST_OFFICIAL_REGION = os.environ.get("PINTEREST_OFFICIAL_REGION") or "US"
PINTEREST_OFFICIAL_INTEREST = os.environ.get("PINTEREST_OFFICIAL_INTEREST") or "beauty"

# YouTube: search.list는 100유닛/회라 주 1회 discovery에서만 사용
YOUTUBE_DISCOVERY_MAX_SEARCHES = 10
YOUTUBE_SEARCH_RESULTS = 25
YOUTUBE_SEARCH_PUBLISHED_WITHIN_DAYS = 30
# 스냅샷 풀에서 제거되기까지의 최대 추적 기간(일)
YOUTUBE_POOL_MAX_AGE_DAYS = 120
YOUTUBE_POOL_MAX_SIZE = 4000


# 검색어트렌드 API의 연령 코드: 1=0-12 2=13-18 3=19-24 4=25-29 5=30-34 6=35-39
# 7=40-44 8=45-49 9=50-54 10=55-59 11=60+. 십대 단위 입력을 코드로 변환한다.
# ("10"은 13-18 코드만 매핑되므로 19세는 20대 구간에 포함되는 근사치)
SEARCH_AGE_CODES = {
    "10": ["2"],
    "20": ["3", "4"],
    "30": ["5", "6"],
    "40": ["7", "8"],
    "50": ["9", "10"],
    "60": ["11"],
}


@dataclass(frozen=True)
class NaverFilters:
    device: str = ""            # "" 전체 | "pc" | "mo"
    gender: str = ""            # "" 전체 | "f" | "m"
    ages: tuple[str, ...] = ()  # 십대 단위 "10"~"60"

    def __post_init__(self) -> None:
        if self.device not in ("", "pc", "mo"):
            raise ValueError(f"filters.device는 ''/pc/mo 중 하나여야 합니다: {self.device!r}")
        if self.gender not in ("", "f", "m"):
            raise ValueError(f"filters.gender는 ''/f/m 중 하나여야 합니다: {self.gender!r}")
        invalid = set(self.ages) - set(SEARCH_AGE_CODES)
        if invalid:
            raise ValueError(f"filters.ages는 10~60(십대 단위)만 허용됩니다: {sorted(invalid)}")

    @property
    def tag(self) -> str:
        """저장 행에 붙는 필터 식별자. 필터가 다른 시계열이 섞이지 않게 한다."""
        if not (self.device or self.gender or self.ages):
            return "all"
        return f"{self.device or '*'}|{self.gender or '*'}|{','.join(self.ages) or '*'}"

    @property
    def search_ages(self) -> list[str]:
        return [code for age in self.ages for code in SEARCH_AGE_CODES[age]]

    @property
    def shopping_ages(self) -> list[str]:
        return list(self.ages)


@dataclass(frozen=True)
class Keyword:
    name: str
    category: str
    synonyms: list[str] = field(default_factory=list)
    shopping: bool = False


@dataclass(frozen=True)
class ShoppingCategory:
    name: str
    cat_id: str


@dataclass(frozen=True)
class KeywordConfig:
    anchor: str
    shopping_anchor: str
    keywords: list[Keyword]
    shopping_categories: list[ShoppingCategory]
    filters: NaverFilters = NaverFilters()

    @property
    def shopping_keywords(self) -> list[Keyword]:
        return [k for k in self.keywords if k.shopping]


@dataclass(frozen=True)
class Channel:
    type: str          # "slack" | "email"
    name: str
    enabled: bool = False
    webhook_env: str = "SLACK_WEBHOOK_URL"  # slack: 이 .env 변수의 URL 사용
    webhook_url: str = ""                    # slack: URL 직접 지정(있으면 우선)
    to: tuple[str, ...] = ()                 # email: 수신자(복수)
    subject: str = ""                        # email: 제목 덮어쓰기(선택)

    def __post_init__(self) -> None:
        if self.type not in ("slack", "email"):
            raise ValueError(f"channel.type은 slack/email 중 하나여야 합니다: {self.type!r}")
        if not self.name:
            raise ValueError("channel.name이 비어 있습니다")


def load_channels_config(path: Path | None = None) -> list[Channel]:
    """리포트 전송 대상 목록. channels.yaml이 없으면 빈 목록(전송 안 함, 출력만)."""
    path = path or PROJECT_ROOT / "channels.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    channels: list[Channel] = []
    for c in raw.get("channels", []):
        channels.append(
            Channel(
                type=str(c["type"]),
                name=str(c["name"]),
                enabled=bool(c.get("enabled", False)),
                webhook_env=str(c.get("webhook_env", "SLACK_WEBHOOK_URL")),
                webhook_url=str(c.get("webhook_url", "")),
                to=tuple(str(x) for x in (c.get("to") or [])),
                subject=str(c.get("subject", "")),
            )
        )
    return channels


def load_keyword_config(path: Path | None = None) -> KeywordConfig:
    path = path or PROJECT_ROOT / "keywords.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    keywords: list[Keyword] = []
    for category, items in raw["keywords"].items():
        for item in items:
            if isinstance(item, str):
                item = {"name": item}
            keywords.append(
                Keyword(
                    name=str(item["name"]),
                    category=category,
                    synonyms=[str(s) for s in item.get("synonyms", [])],
                    shopping=bool(item.get("shopping", False)),
                )
            )

    categories = [
        ShoppingCategory(name=c["name"], cat_id=str(c["cat_id"]))
        for c in raw.get("shopping_categories", [])
    ]
    f = raw.get("filters") or {}
    filters = NaverFilters(
        device=str(f.get("device") or ""),
        gender=str(f.get("gender") or ""),
        ages=tuple(str(a) for a in (f.get("ages") or [])),
    )
    return KeywordConfig(
        anchor=str(raw["anchor"]),
        shopping_anchor=str(raw.get("shopping_anchor", raw["anchor"])),
        keywords=keywords,
        shopping_categories=categories,
        filters=filters,
    )
