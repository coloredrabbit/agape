from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb

from . import storage

# 판정 임계값
Z_HOT = 2.0            # 4주 이동평균 대비 z-score 급등 기준
VELOCITY_RISING = 0.15  # WoW 15% 이상이면 상승으로 판정
YT_VELOCITY_HOT = 0.5   # 유튜브 단독 급등 기준
YOY_NEW_TREND = 0.5     # 전년 동기 대비 50% 이상 성장해야 '신규 트렌드' (미만이면 계절성 의심)
PIN_MOM_RISING = 0.15   # 핀터레스트 월간 15% 이상이면 상승으로 판정
PIN_MOM_HOT = 0.5       # 핀터레스트 단독 급등 기준

# 핀터레스트 급상승 후보 중 헤어 관련만 추리기 위한 포함어
HAIR_TERMS = [
    "머리", "헤어", "컷", "펌", "염색", "탈색", "단발", "장발", "뱅", "앞머리",
    "가르마", "레이어드", "울프", "보브", "히피", "매직", "브라운", "발레아쥬",
    "hair", "haircut", "perm", "bangs", "bob",
]


@dataclass
class KeywordMetrics:
    keyword: str
    naver_vol: float | None = None
    naver_velocity: float | None = None
    naver_z: float | None = None
    naver_yoy: float | None = None
    yt_views: int | None = None
    yt_velocity: float | None = None
    pin_wow: float | None = None
    pin_mom: float | None = None

    @property
    def signal(self) -> str:
        z = self.naver_z
        naver_hot = z is not None and z >= Z_HOT
        naver_rising = self.naver_velocity is not None and self.naver_velocity >= VELOCITY_RISING
        yt_rising = self.yt_velocity is not None and self.yt_velocity >= VELOCITY_RISING
        yt_hot = self.yt_velocity is not None and self.yt_velocity >= YT_VELOCITY_HOT
        pin_rising = self.pin_mom is not None and self.pin_mom >= PIN_MOM_RISING
        pin_hot = self.pin_mom is not None and self.pin_mom >= PIN_MOM_HOT

        if naver_hot and self.naver_yoy is not None and self.naver_yoy < YOY_NEW_TREND:
            return "계절성 의심"
        if naver_hot and naver_rising and (yt_rising or pin_rising):
            return "강한 후보"
        if naver_hot or yt_hot or pin_hot:
            return "관찰"
        return ""

    @property
    def sort_key(self) -> tuple:
        order = {"강한 후보": 0, "관찰": 1, "계절성 의심": 2, "": 3}
        return (order[self.signal], -(self.naver_z if self.naver_z is not None else float("-inf")))


def last_complete_week(today: date | None = None) -> date:
    """직전 완결 ISO 주의 월요일. 진행 중인 주는 부분 데이터라 집계에서 제외한다."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7)


def _naver_weekly(
    con: duckdb.DuckDBPyConnection, week: date, filters_tag: str
) -> dict[str, dict[str, Any]]:
    """filters_tag가 일치하는 행만 집계 — 필터가 다른 시계열은 스케일이 달라 섞으면 안 된다."""
    if not storage.has_data("naver_search"):
        return {}
    rows = con.execute(
        """
        WITH raw AS (
            SELECT keyword, CAST(date AS DATE) AS d, ratio_adj,
                   ROW_NUMBER() OVER (PARTITION BY keyword, date ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?, union_by_name=true)
            WHERE ratio_adj IS NOT NULL AND coalesce(filters, 'all') = ?
        ),
        weekly AS (
            SELECT keyword, CAST(date_trunc('week', d) AS DATE) AS week, avg(ratio_adj) AS vol
            FROM raw WHERE rn = 1
            GROUP BY 1, 2
        ),
        w AS (
            SELECT keyword, week, vol,
                   lag(vol) OVER win AS prev_vol,
                   avg(vol) OVER (win ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS ma4,
                   stddev_samp(vol) OVER (win ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS sd4,
                   lag(vol, 52) OVER win AS yoy_vol
            FROM weekly
            WINDOW win AS (PARTITION BY keyword ORDER BY week)
        )
        SELECT keyword, vol,
               (vol - prev_vol) / nullif(prev_vol, 0) AS velocity,
               (vol - ma4) / nullif(sd4, 0) AS z,
               (vol - yoy_vol) / nullif(yoy_vol, 0) AS yoy
        FROM w
        WHERE week = ?
        """,
        [storage.source_glob("naver_search"), filters_tag, week],
    ).fetchall()
    return {
        r[0]: {"vol": r[1], "velocity": r[2], "z": r[3], "yoy": r[4]} for r in rows
    }


def _youtube_weekly(con: duckdb.DuckDBPyConnection, week: date) -> dict[str, dict[str, Any]]:
    if not storage.has_data("youtube_stats"):
        return {}
    rows = con.execute(
        """
        WITH raw AS (
            SELECT video_id, CAST(date AS DATE) AS d, view_count, keywords,
                   ROW_NUMBER() OVER (PARTITION BY video_id, date ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?)
        ),
        delta AS (
            SELECT video_id, d, keywords,
                   view_count - lag(view_count) OVER (PARTITION BY video_id ORDER BY d) AS dviews
            FROM raw WHERE rn = 1
        ),
        exploded AS (
            SELECT unnest(keywords) AS keyword, d, greatest(dviews, 0) AS dviews
            FROM delta WHERE dviews IS NOT NULL
        ),
        weekly AS (
            SELECT keyword, CAST(date_trunc('week', d) AS DATE) AS week, sum(dviews) AS views
            FROM exploded GROUP BY 1, 2
        ),
        w AS (
            SELECT keyword, week, views,
                   lag(views) OVER (PARTITION BY keyword ORDER BY week) AS prev_views
            FROM weekly
        )
        SELECT keyword, views, (views - prev_views) / nullif(prev_views, 0) AS velocity
        FROM w
        WHERE week = ?
        """,
        [storage.source_glob("youtube_stats"), week],
    ).fetchall()
    return {r[0]: {"views": r[1], "velocity": r[2]} for r in rows}


def _pinterest_latest(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    """키워드별 최신 수집분의 핀터레스트 성장률. 주간 집계라 target week와 무관하게 최신본 사용."""
    if not storage.has_data("pinterest_metrics"):
        return {}
    rows = con.execute(
        """
        SELECT keyword, wow, mom FROM (
            SELECT keyword, wow, mom,
                   ROW_NUMBER() OVER (PARTITION BY keyword ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?)
        ) WHERE rn = 1
        """,
        [storage.source_glob("pinterest_metrics")],
    ).fetchall()
    return {r[0]: {"wow": r[1], "mom": r[2]} for r in rows}


def pinterest_new_candidates(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """핀터레스트 급상승 톱N 중 사전에 없는 헤어 관련 키워드 후보."""
    if not storage.has_data("pinterest_top"):
        return []
    rows = con.execute(
        """
        SELECT term, rank, wow, mom, in_dictionary FROM (
            SELECT term, rank, wow, mom, in_dictionary,
                   ROW_NUMBER() OVER (PARTITION BY term ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?)
        ) WHERE rn = 1
        ORDER BY rank
        """,
        [storage.source_glob("pinterest_top")],
    ).fetchall()
    candidates = []
    for term, rank, wow, mom, in_dict in rows:
        if in_dict:
            continue
        if any(t in term.lower() for t in HAIR_TERMS):
            candidates.append({"term": term, "rank": rank, "wow": wow, "mom": mom})
    return candidates[:10]


def pinterest_official_topics(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """공식 API로 받은 US/글로벌 트렌딩 토픽 (최신 수집분)."""
    if not storage.has_data("pinterest_official_topics"):
        return []
    rows = con.execute(
        """
        SELECT region, interest, title, pct_growth_mom FROM (
            SELECT region, interest, title, pct_growth_mom, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY title ORDER BY fetched_at DESC) AS rn,
                   max(fetched_at) OVER () AS latest
            FROM read_ndjson_auto(?, union_by_name=true)
        ) WHERE rn = 1 AND fetched_at = latest
        ORDER BY pct_growth_mom DESC NULLS LAST
        """,
        [storage.source_glob("pinterest_official_topics")],
    ).fetchall()
    return [
        {"region": r[0], "interest": r[1], "title": r[2], "pct_growth_mom": r[3]} for r in rows
    ]


def shopping_category_summary(
    con: duckdb.DuckDBPyConnection, week: date, filters_tag: str
) -> list[dict[str, Any]]:
    if not storage.has_data("naver_shopping"):
        return []
    rows = con.execute(
        """
        WITH raw AS (
            SELECT keyword, CAST(date AS DATE) AS d, ratio,
                   ROW_NUMBER() OVER (PARTITION BY keyword, date ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?, union_by_name=true)
            WHERE kind = 'category' AND coalesce(filters, 'all') = ?
        ),
        weekly AS (
            SELECT keyword, CAST(date_trunc('week', d) AS DATE) AS week, avg(ratio) AS vol
            FROM raw WHERE rn = 1
            GROUP BY 1, 2
        ),
        w AS (
            SELECT keyword, week, vol,
                   lag(vol) OVER (PARTITION BY keyword ORDER BY week) AS prev_vol
            FROM weekly
        )
        SELECT keyword, vol, (vol - prev_vol) / nullif(prev_vol, 0) AS velocity
        FROM w WHERE week = ?
        """,
        [storage.source_glob("naver_shopping"), filters_tag, week],
    ).fetchall()
    return [{"category": r[0], "vol": r[1], "velocity": r[2]} for r in rows]


def latest_trending_hair(con: duckdb.DuckDBPyConnection, keyword_names: list[str]) -> list[dict[str, Any]]:
    """최신 인기 급상승 차트에서 헤어 관련 제목만 추린다."""
    if not storage.has_data("youtube_trending"):
        return []
    rows = con.execute(
        """
        SELECT rank, title, channel, view_count
        FROM read_ndjson_auto(?)
        WHERE date = (SELECT max(date) FROM read_ndjson_auto(?))
        ORDER BY rank
        """,
        [storage.source_glob("youtube_trending"), storage.source_glob("youtube_trending")],
    ).fetchall()
    terms = ["머리", "헤어", "미용", "펌", "염색", "커트", "컷"] + keyword_names
    hits = []
    for rank, title, channel, views in rows:
        if any(t in title for t in terms):
            hits.append({"rank": rank, "title": title, "channel": channel, "views": views})
    return hits


def compute(
    keyword_names: list[str], week: date | None = None, filters_tag: str = "all"
) -> dict[str, Any]:
    week = week or last_complete_week()
    con = duckdb.connect()
    naver = _naver_weekly(con, week, filters_tag)
    youtube = _youtube_weekly(con, week)
    pinterest = _pinterest_latest(con)

    metrics: list[KeywordMetrics] = []
    for name in sorted(set(keyword_names) | set(naver) | set(youtube) | set(pinterest)):
        n = naver.get(name, {})
        y = youtube.get(name, {})
        p = pinterest.get(name, {})
        metrics.append(
            KeywordMetrics(
                keyword=name,
                naver_vol=n.get("vol"),
                naver_velocity=n.get("velocity"),
                naver_z=n.get("z"),
                naver_yoy=n.get("yoy"),
                yt_views=int(y["views"]) if y.get("views") is not None else None,
                yt_velocity=y.get("velocity"),
                pin_wow=p.get("wow"),
                pin_mom=p.get("mom"),
            )
        )
    metrics.sort(key=lambda m: m.sort_key)

    return {
        "week": week,
        "metrics": metrics,
        "shopping": shopping_category_summary(con, week, filters_tag),
        "trending": latest_trending_hair(con, keyword_names),
        "pinterest_candidates": pinterest_new_candidates(con),
        "pinterest_official": pinterest_official_topics(con),
    }
