"""Pinterest 공식 Trends API 수집기 (client_credentials 방식).

US/글로벌 선행 신호용이다 — 공식 API는 한국(KR)을 지원하지 않는다(US/EU/중남미 등만).
KR 데이터는 pinterest.py 크롤러가 담당하고, 이 수집기는 그와 별개로 안정적·합법적인
글로벌 신호를 보완한다.

인증: App ID/Secret으로 client_credentials 토큰을 발급받는다(OAuth 리다이렉트 불필요).
단, Trends 데이터 접근은 Pinterest 파트너/승인이 필요할 수 있어 미승인 계정은 403을 받는다 —
이 경우 명확히 안내하고 건너뛴다(cli.collect가 소스별 독립 실행이라 다른 소스에 영향 없음).
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import (
    PINTEREST_APP_ID,
    PINTEREST_APP_SECRET,
    PINTEREST_OFFICIAL_INTEREST,
    PINTEREST_OFFICIAL_REGION,
)
from .. import storage

API_BASE = "https://api.pinterest.com/v5"
TOKEN_URL = "https://api.pinterest.com/v3/oauth/access_token/"
TOKEN_STATE = "pinterest_official_token"
SCOPE = "user_accounts:read"


def _access_token(client: httpx.Client) -> str:
    """캐시된 토큰이 유효하면 재사용, 아니면 client_credentials로 재발급."""
    cached = storage.load_state(TOKEN_STATE, None)
    if cached and cached.get("expires_at", 0) > time.time() + 60:
        return cached["access_token"]

    if not (PINTEREST_APP_ID and PINTEREST_APP_SECRET):
        raise SystemExit("PINTEREST_APP_ID / PINTEREST_APP_SECRET가 설정되지 않았습니다 (.env 확인)")

    basic = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    resp = client.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"Pinterest 토큰 발급 실패({resp.status_code}) — App ID/Secret 또는 앱 권한 확인: "
            f"{resp.text[:200]}"
        )
    payload = resp.json()
    data = payload.get("data", payload)  # v3는 {data:{...}} 래핑 가능성 대비
    access = data.get("access_token")
    if not access:
        raise SystemExit(f"토큰 응답에 access_token 없음: {str(payload)[:200]}")
    storage.save_state(
        TOKEN_STATE,
        {"access_token": access, "expires_at": time.time() + int(data.get("expires_in", 3600))},
    )
    return access


def _get(client: httpx.Client, token: str, path: str, params: dict[str, Any]) -> Any:
    resp = client.get(
        f"{API_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"Trends API 접근 거부({resp.status_code}) — Pinterest Trends 데이터는 파트너/승인이 "
            f"필요할 수 있습니다: {resp.text[:150]}"
        )
    resp.raise_for_status()
    return resp.json()


def collect() -> int:
    region = PINTEREST_OFFICIAL_REGION
    interest = PINTEREST_OFFICIAL_INTEREST
    fetched_at = datetime.now(timezone.utc).isoformat()

    with httpx.Client() as client:
        token = _access_token(client)

        # 1) 관심사(beauty) 트렌딩 토픽 — 웹 UI와 동일한 선행 신호
        topics = _get(
            client, token, "/trends/topics/featured", {"region": region, "interest": interest}
        )
        topic_rows: list[dict[str, Any]] = []
        for group in topics if isinstance(topics, list) else []:
            for t in group.get("trends", []):
                topic_rows.append(
                    {
                        "region": region,
                        "interest": group.get("interest", interest),
                        "market": group.get("market", region),
                        "title": t.get("title"),
                        "description": t.get("description"),
                        "pct_growth_mom": t.get("percent_growth_mom"),
                        "time_series": t.get("time_series"),
                        "fetched_at": fetched_at,
                    }
                )

        # 2) 성장 중인 쇼핑 제품 카테고리 (뷰티 vertical 포함)
        cats = _get(
            client,
            token,
            "/trends/product_categories/trending",
            {"region": region},
        )
        cat_rows: list[dict[str, Any]] = []
        for c in cats if isinstance(cats, list) else []:
            cat_rows.append(
                {
                    "region": region,
                    "product_category": c.get("product_category"),
                    "pct_change_mom": c.get("pct_change_mom"),
                    "percent_relative_volume": c.get("percent_relative_volume"),
                    "verticals": c.get("verticals"),
                    "engagement_type": c.get("engagement_type"),
                    "fetched_at": fetched_at,
                }
            )

    storage.write_jsonl("pinterest_official_topics", topic_rows)
    storage.write_jsonl("pinterest_official_categories", cat_rows)
    print(
        f"[pinterest_official] region={region} interest={interest}: "
        f"topics {len(topic_rows)}, categories {len(cat_rows)}"
    )
    return len(topic_rows)
