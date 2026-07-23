"""Pinterest Trends(trends.pinterest.com) 크롤링 수집기.

비공식 내부 API를 사용한다 — 로그인 불필요한 공개 집계 데이터이며 개인정보를 다루지 않는다.
스펙이 예고 없이 바뀔 수 있으므로 이 수집기의 실패가 다른 소스에 영향을 주지 않아야 하고
(cli.collect가 소스별 독립 실행), 모든 요청 전에 robots.txt 가드를 통과해야 한다.
데이터는 주간 집계라 latest_available_date가 바뀌었을 때만 실제 수집한다.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ..config import (
    PINTEREST_COOKIE,
    PINTEREST_COUNTRY,
    PINTEREST_CRAWLER_UA,
    PINTEREST_LOOKBACK_DAYS,
    PINTEREST_METRICS_BATCH,
    PINTEREST_TOP_N,
    KeywordConfig,
)
from ..robots import RobotsGuard
from .. import storage

BASE_URL = "https://trends.pinterest.com"
LAST_DATE_STATE = "pinterest_last_end_date"
REQUEST_INTERVAL_SEC = 1.0


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": PINTEREST_CRAWLER_UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "application/json",
    }
    if PINTEREST_COOKIE:
        headers["Cookie"] = PINTEREST_COOKIE
    return headers


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _get(client: httpx.Client, guard: RobotsGuard, path: str, params: dict[str, Any]) -> Any:
    url = f"{BASE_URL}{path}"
    guard.ensure_allowed(url)
    resp = client.get(url, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_INTERVAL_SEC)
    return resp.json()


def _growth(item: dict[str, Any], key: str) -> float | None:
    value = (item.get("growth_rates") or {}).get(key)
    return value


def collect(cfg: KeywordConfig) -> int:
    guard = RobotsGuard(PINTEREST_CRAWLER_UA)
    fetched_at = datetime.now(timezone.utc).isoformat()

    with httpx.Client() as client:
        latest = _get(client, guard, "/latest_available_date/", {})["date"]

        if storage.load_state(LAST_DATE_STATE, None) == latest and storage.has_data(
            "pinterest_metrics"
        ):
            print(f"[pinterest] 최신 데이터({latest}) 이미 수집됨 — 건너뜀")
            return 0

        # 1) 사전 키워드 전체의 주간 시계열 + 성장률
        metric_rows: list[dict[str, Any]] = []
        names = [k.name for k in cfg.keywords]
        for batch in _chunks(names, PINTEREST_METRICS_BATCH):
            payload = _get(
                client,
                guard,
                "/metrics/",
                {
                    "terms": ",".join(batch),
                    "country": PINTEREST_COUNTRY,
                    "end_date": latest,
                    "days": PINTEREST_LOOKBACK_DAYS,
                    "aggregation": 2,  # 주간
                    "normalize_against_group": "false",
                    "predicted_days": 0,
                },
            )
            for item in payload:
                metric_rows.append(
                    {
                        "keyword": item["term"],
                        "end_date": latest,
                        "wow": _growth(item, "wow_change"),
                        "mom": _growth(item, "mom_change"),
                        "yoy": _growth(item, "yoy_change"),
                        "series": [
                            {"date": c["date"], "count": c.get("normalizedCount")}
                            for c in item.get("counts", [])
                        ],
                        "fetched_at": fetched_at,
                    }
                )

        # 2) 급상승 톱N — 사전에 없는 신규 키워드 후보 발굴용
        top_payload = _get(
            client,
            guard,
            "/top_trends_filtered/",
            {
                "lookbackWindow": 2,
                "endDate": latest,
                "rankingMethod": 3,
                "country": PINTEREST_COUNTRY,
                "trendsPreset": 3,
                "numTermsToReturn": PINTEREST_TOP_N,
            },
        )
        known = {k.name for k in cfg.keywords}
        top_rows = []
        for rank, item in enumerate(top_payload.get("values", []), start=1):
            top_rows.append(
                {
                    "term": item["term"],
                    "rank": rank,
                    "wow": (item.get("wow_change") or {}).get("value"),
                    "mom": (item.get("mom_change") or {}).get("value"),
                    "yoy": (item.get("yoy_change") or {}).get("value"),
                    "seasonality": item.get("seasonality_score"),
                    "search_count": item.get("searchCount"),
                    "in_dictionary": item["term"] in known,
                    "end_date": latest,
                    "fetched_at": fetched_at,
                }
            )

    storage.write_jsonl("pinterest_metrics", metric_rows)
    storage.write_jsonl("pinterest_top", top_rows)
    storage.save_state(LAST_DATE_STATE, latest)
    print(f"[pinterest] {len(metric_rows)} keywords, top {len(top_rows)} (end_date={latest})")
    return len(metric_rows)
