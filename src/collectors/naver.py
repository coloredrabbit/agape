from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from ..config import (
    NAVER_BACKFILL_DAYS,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    NAVER_GROUPS_PER_REQUEST,
    NAVER_INCREMENTAL_DAYS,
    Keyword,
    KeywordConfig,
    NaverFilters,
)
from .. import storage

SEARCH_URL = "https://openapi.naver.com/v1/datalab/search"
SHOP_CATEGORY_URL = "https://openapi.naver.com/v1/datalab/shopping/categories"
SHOP_KEYWORD_URL = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"

ANCHOR_GROUP = "_anchor"
REQUEST_INTERVAL_SEC = 0.25
FILTERS_TAG_STATE = "naver_filters_tag"


def _search_filter_fields(f: NaverFilters) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if f.device:
        fields["device"] = f.device
    if f.gender:
        fields["gender"] = f.gender
    if f.ages:
        fields["ages"] = f.search_ages
    return fields


def _shopping_filter_fields(f: NaverFilters) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if f.device:
        fields["device"] = f.device
    if f.gender:
        fields["gender"] = f.gender
    if f.ages:
        fields["ages"] = f.shopping_ages
    return fields


def _headers() -> dict[str, str]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise SystemExit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET가 설정되지 않았습니다 (.env 확인)")
    return {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json",
    }


def _chunks(items: list[Keyword], size: int) -> Iterator[list[Keyword]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _post(client: httpx.Client, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(url, json=body, headers=_headers(), timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_INTERVAL_SEC)
    return resp.json()


def _rescale_rows(
    payload: dict[str, Any], batch_id: int, source_meta: dict[str, Any]
) -> list[dict[str, Any]]:
    """앵커 그룹 평균으로 나눠 배치 간 비교 가능한 ratio_adj를 만든다."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    results = payload.get("results", [])
    anchor_values = [
        p["ratio"] for r in results if r["title"] == ANCHOR_GROUP for p in r["data"]
    ]
    anchor_mean = sum(anchor_values) / len(anchor_values) if anchor_values else None

    rows: list[dict[str, Any]] = []
    for r in results:
        if r["title"] == ANCHOR_GROUP:
            continue
        for point in r["data"]:
            ratio = point["ratio"]
            rows.append(
                {
                    "keyword": r["title"],
                    "date": point["period"],
                    "ratio": ratio,
                    "ratio_adj": (ratio / anchor_mean) if anchor_mean else None,
                    "anchor_mean": anchor_mean,
                    "batch_id": batch_id,
                    "fetched_at": fetched_at,
                    **source_meta,
                }
            )
    return rows


def collect_search_trend(cfg: KeywordConfig) -> int:
    """데이터랩 검색어트렌드. 최초 실행 또는 필터 변경 시 자동 백필, 이후 최근 N일 재수집."""
    tag = cfg.filters.tag
    stored_tag = storage.load_state(FILTERS_TAG_STATE, None)
    backfill = not storage.has_data("naver_search") or stored_tag != tag
    if backfill and stored_tag is not None and stored_tag != tag:
        print(f"[naver_search] 필터 변경 감지({stored_tag} → {tag}) — 새 태그로 재백필")
    days = NAVER_BACKFILL_DAYS if backfill else NAVER_INCREMENTAL_DAYS
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days)

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for batch_id, batch in enumerate(_chunks(cfg.keywords, NAVER_GROUPS_PER_REQUEST)):
            groups = [{"groupName": ANCHOR_GROUP, "keywords": [cfg.anchor]}] + [
                {"groupName": kw.name, "keywords": ([kw.name] + kw.synonyms)[:20]}
                for kw in batch
            ]
            body = {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "timeUnit": "date",
                "keywordGroups": groups,
                **_search_filter_fields(cfg.filters),
            }
            payload = _post(client, SEARCH_URL, body)
            rows.extend(
                _rescale_rows(payload, batch_id, {"anchor": cfg.anchor, "filters": tag})
            )

    storage.write_jsonl("naver_search", rows)
    storage.save_state(FILTERS_TAG_STATE, tag)
    mode = "backfill" if backfill else "incremental"
    print(f"[naver_search] {mode}: {len(rows)} rows ({start} ~ {end}, filters={tag})")
    return len(rows)


def collect_shopping(cfg: KeywordConfig) -> int:
    """쇼핑인사이트: 카테고리 클릭 트렌드 + shopping=true 키워드 클릭 트렌드."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=NAVER_INCREMENTAL_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        if cfg.shopping_categories:
            body = {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "timeUnit": "date",
                "category": [
                    {"name": c.name, "param": [c.cat_id]} for c in cfg.shopping_categories[:3]
                ],
                **_shopping_filter_fields(cfg.filters),
            }
            payload = _post(client, SHOP_CATEGORY_URL, body)
            for r in payload.get("results", []):
                for point in r["data"]:
                    rows.append(
                        {
                            "kind": "category",
                            "keyword": r["title"],
                            "date": point["period"],
                            "ratio": point["ratio"],
                            "ratio_adj": None,
                            "filters": cfg.filters.tag,
                            "fetched_at": fetched_at,
                        }
                    )

        cat_id = cfg.shopping_categories[0].cat_id if cfg.shopping_categories else None
        if cat_id and cfg.shopping_keywords:
            for batch_id, batch in enumerate(
                _chunks(cfg.shopping_keywords, NAVER_GROUPS_PER_REQUEST)
            ):
                keyword_param = [{"name": ANCHOR_GROUP, "param": [cfg.shopping_anchor]}] + [
                    {"name": kw.name, "param": [kw.name]} for kw in batch
                ]
                body = {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "timeUnit": "date",
                    "category": cat_id,
                    "keyword": keyword_param,
                    **_shopping_filter_fields(cfg.filters),
                }
                payload = _post(client, SHOP_KEYWORD_URL, body)
                shop_rows = _rescale_rows(
                    payload, batch_id, {"anchor": cfg.shopping_anchor, "filters": cfg.filters.tag}
                )
                for row in shop_rows:
                    row["kind"] = "keyword"
                rows.extend(shop_rows)

    storage.write_jsonl("naver_shopping", rows)
    print(f"[naver_shopping] {len(rows)} rows ({start} ~ {end}, filters={cfg.filters.tag})")
    return len(rows)


def query(
    terms: list[str],
    start: date,
    end: date,
    time_unit: str,
    filters: NaverFilters,
) -> dict[str, Any]:
    """애드혹 조회 — 저장하지 않고 결과만 반환.

    한 요청(최대 5그룹)에 모든 키워드를 넣으므로 키워드 간 ratio 직접 비교가 가능하다
    (파이프라인과 달리 앵커 재보정 불필요).
    """
    if not 1 <= len(terms) <= 5:
        raise SystemExit("query는 키워드 1~5개만 지원합니다 (데이터랩 요청당 5그룹 제한)")
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": time_unit,
        "keywordGroups": [{"groupName": t, "keywords": [t]} for t in terms],
        **_search_filter_fields(filters),
    }
    with httpx.Client() as client:
        try:
            payload = _post(client, SEARCH_URL, body)
        except httpx.HTTPStatusError as e:
            raise SystemExit(f"데이터랩 오류 {e.response.status_code}: {e.response.text}") from e

    series: dict[str, dict[str, float]] = {}
    for r in payload.get("results", []):
        series[r["title"]] = {p["period"]: p["ratio"] for p in r["data"]}
    return {"series": series, "terms": terms}
