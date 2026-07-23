from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import (
    YOUTUBE_API_KEY,
    YOUTUBE_DISCOVERY_MAX_SEARCHES,
    YOUTUBE_POOL_MAX_AGE_DAYS,
    YOUTUBE_POOL_MAX_SIZE,
    YOUTUBE_SEARCH_PUBLISHED_WITHIN_DAYS,
    YOUTUBE_SEARCH_RESULTS,
    KeywordConfig,
)
from .. import storage

BASE_URL = "https://www.googleapis.com/youtube/v3"
POOL_STATE = "youtube_video_pool"
REQUEST_INTERVAL_SEC = 0.1
# Howto & Style — 한국 뷰티/헤어 튜토리얼 대부분이 속하는 카테고리
TRENDING_CATEGORY_ID = "26"


def _key() -> str:
    if not YOUTUBE_API_KEY:
        raise SystemExit("YOUTUBE_API_KEY가 설정되지 않았습니다 (.env 확인)")
    return YOUTUBE_API_KEY


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = client.get(f"{BASE_URL}/{path}", params={**params, "key": _key()}, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_INTERVAL_SEC)
    return resp.json()


def _load_pool() -> dict[str, dict[str, Any]]:
    return storage.load_state(POOL_STATE, {})


def _prune_pool(pool: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=YOUTUBE_POOL_MAX_AGE_DAYS)).isoformat()
    kept = {vid: meta for vid, meta in pool.items() if meta["first_seen"] >= cutoff}
    if len(kept) > YOUTUBE_POOL_MAX_SIZE:
        newest = sorted(kept.items(), key=lambda kv: kv[1]["first_seen"], reverse=True)
        kept = dict(newest[:YOUTUBE_POOL_MAX_SIZE])
    return kept


def discover(cfg: KeywordConfig) -> int:
    """search.list(100유닛/회)로 키워드별 최신 영상을 풀에 추가. 주 1회 실행 전제.

    쿼터를 아끼기 위해 전체 키워드를 ISO 주차 기준으로 순환하며 일부만 검색한다.
    """
    pool = _load_pool()
    week = date.today().isocalendar().week
    n = len(cfg.keywords)
    offset = (week * YOUTUBE_DISCOVERY_MAX_SEARCHES) % n
    selected = [cfg.keywords[(offset + i) % n] for i in range(min(YOUTUBE_DISCOVERY_MAX_SEARCHES, n))]

    published_after = (
        datetime.now(timezone.utc) - timedelta(days=YOUTUBE_SEARCH_PUBLISHED_WITHIN_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    added = 0
    with httpx.Client() as client:
        for kw in selected:
            payload = _get(
                client,
                "search",
                {
                    "part": "snippet",
                    "q": kw.name,
                    "type": "video",
                    "regionCode": "KR",
                    "relevanceLanguage": "ko",
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "maxResults": YOUTUBE_SEARCH_RESULTS,
                },
            )
            for item in payload.get("items", []):
                vid = item["id"]["videoId"]
                snippet = item["snippet"]
                if vid not in pool:
                    pool[vid] = {
                        "keywords": [],
                        "title": snippet["title"],
                        "channel": snippet["channelTitle"],
                        "published_at": snippet["publishedAt"],
                        "first_seen": date.today().isoformat(),
                    }
                    added += 1
                if kw.name not in pool[vid]["keywords"]:
                    pool[vid]["keywords"].append(kw.name)

    pool = _prune_pool(pool)
    storage.save_state(POOL_STATE, pool)
    print(
        f"[youtube_discover] searched {len(selected)} keywords "
        f"({', '.join(k.name for k in selected)}), +{added} videos, pool={len(pool)}"
    )
    return added


def snapshot() -> int:
    """videos.list(1유닛/50개)로 풀 전체의 조회수 스냅샷을 찍는다. 일 1회 실행 전제."""
    pool = _prune_pool(_load_pool())
    storage.save_state(POOL_STATE, pool)
    ids = list(pool.keys())
    if not ids:
        print("[youtube_snapshot] 영상 풀이 비어 있습니다. 먼저 `agape discover`를 실행하세요.")
        return 0

    fetched_at = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for i in range(0, len(ids), 50):
            payload = _get(
                client,
                "videos",
                {"part": "statistics", "id": ",".join(ids[i : i + 50]), "maxResults": 50},
            )
            for item in payload.get("items", []):
                stats = item.get("statistics", {})
                rows.append(
                    {
                        "video_id": item["id"],
                        "date": today,
                        "view_count": int(stats.get("viewCount", 0)),
                        "like_count": int(stats.get("likeCount", 0)) if "likeCount" in stats else None,
                        "comment_count": int(stats.get("commentCount", 0)) if "commentCount" in stats else None,
                        "keywords": pool[item["id"]]["keywords"] if item["id"] in pool else [],
                        "fetched_at": fetched_at,
                    }
                )

    storage.write_jsonl("youtube_stats", rows)
    print(f"[youtube_snapshot] {len(rows)} videos")
    return len(rows)


def trending() -> int:
    """한국 인기 급상승(Howto & Style) 차트. 1유닛."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        payload = _get(
            client,
            "videos",
            {
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": "KR",
                "videoCategoryId": TRENDING_CATEGORY_ID,
                "maxResults": 50,
            },
        )
        for rank, item in enumerate(payload.get("items", []), start=1):
            rows.append(
                {
                    "video_id": item["id"],
                    "date": today,
                    "rank": rank,
                    "title": item["snippet"]["title"],
                    "channel": item["snippet"]["channelTitle"],
                    "view_count": int(item.get("statistics", {}).get("viewCount", 0)),
                    "fetched_at": fetched_at,
                }
            )

    storage.write_jsonl("youtube_trending", rows)
    print(f"[youtube_trending] {len(rows)} videos")
    return len(rows)
