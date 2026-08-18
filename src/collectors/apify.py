"""Apify 인스타그램 수집기 — 사용자가 만든 task의 결과 데이터셋을 읽어온다.

크레딧 정책: 이 수집기는 **run을 트리거하지 않는다**. `runs/last/dataset/items`로
마지막 성공 run의 결과만 읽으므로 무료 크레딧을 소모하지 않는다. 새로 긁고 싶으면
Apify 콘솔에서 직접 실행하거나 `agape apify-run`(수동 전용)을 쓴다.

액터마다 출력 필드명이 달라서(`likesCount` vs `likeCount` 등) _norm()에서 방어적으로
매핑한다. 처음 연결할 때 `agape apify-peek`으로 실제 필드를 확인하고 조정할 것.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import (
    APIFY_DATASET_LIMIT,
    APIFY_INSTAGRAM_TASK_ID,
    APIFY_STALE_SKIP_HOURS,
    APIFY_STALE_WARN_HOURS,
    APIFY_TOKEN,
)
from .. import storage

API_BASE = "https://api.apify.com/v2"


def _headers() -> dict[str, str]:
    if not APIFY_TOKEN:
        raise SystemExit("APIFY_TOKEN이 설정되지 않았습니다 (.env 확인)")
    # 토큰은 헤더로만 보낸다 — URL 쿼리에 넣으면 로그/히스토리에 남는다
    return {"Authorization": f"Bearer {APIFY_TOKEN}", "Accept": "application/json"}


def _task_id() -> str:
    if not APIFY_INSTAGRAM_TASK_ID:
        raise SystemExit("APIFY_INSTAGRAM_TASK_ID가 설정되지 않았습니다 (.env 확인)")
    return APIFY_INSTAGRAM_TASK_ID


def _first(item: dict[str, Any], *keys: str) -> Any:
    """액터별 필드명 차이를 흡수한다 — 먼저 존재하는 키의 값을 반환."""
    for k in keys:
        v = item.get(k)
        if v is not None:
            return v
    return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_reel(item: dict[str, Any]) -> bool:
    """릴스 판정. productType이 있으면 그것을 우선하고, 없으면 동영상 여부로 근사한다."""
    product = (_first(item, "productType", "product_type") or "")
    if product:
        return str(product).lower() in ("clips", "reel", "reels")
    kind = str(_first(item, "type", "mediaType", "__typename") or "").lower()
    return "video" in kind or "clip" in kind


def _music(item: dict[str, Any]) -> str | None:
    """musicInfo → "아티스트 - 곡명" 한 줄. 트렌딩 오디오 추적용.

    원본 오디오를 쓴 경우엔 곡 정보가 비어 있으므로 그렇게 표기한다.
    """
    info = item.get("musicInfo")
    if not isinstance(info, dict):
        return None
    artist = (info.get("artist_name") or "").strip()
    song = (info.get("song_name") or "").strip()
    if song or artist:
        return " - ".join(p for p in (artist, song) if p)
    return "원본 오디오" if info.get("uses_original_audio") else None


def _norm(item: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    """액터 출력 1건을 파이프라인 공통 스키마로 정규화."""
    shortcode = _first(item, "shortCode", "shortcode", "code")
    url = _first(item, "url", "postUrl", "link")
    if not url and shortcode:
        url = f"https://www.instagram.com/reel/{shortcode}/"
    caption = _first(item, "caption", "text", "description") or ""
    tags = _first(item, "hashtags")
    return {
        "post_id": _first(item, "id", "postId", "pk"),
        "shortcode": shortcode,
        "url": url,
        "caption": caption,
        "owner": _first(item, "ownerUsername", "username", "owner_username"),
        "posted_at": _first(item, "timestamp", "takenAt", "taken_at_timestamp"),
        "likes": _as_int(_first(item, "likesCount", "likeCount", "likes")),
        "comments": _as_int(_first(item, "commentsCount", "commentCount", "comments")),
        # views = 재생 수(반복 포함), view_count = 고유 시청 추정치. 실측 3.7배까지 벌어지므로
        # 둘을 따로 저장해 반복 시청률을 볼 수 있게 한다.
        "views": _as_int(_first(item, "videoPlayCount", "playCount", "views")),
        "view_count": _as_int(_first(item, "videoViewCount", "viewCount")),
        "duration_sec": _first(item, "videoDuration", "duration"),
        "music": _music(item),
        # 고정 게시물은 노출이 누적돼 뷰가 비정상적으로 높다 — 순위에서 제외하기 위해 저장.
        "is_pinned": bool(_first(item, "isPinned", "is_pinned") or False),
        "is_reel": _is_reel(item),
        "hashtags": tags if isinstance(tags, list) else [],
        "fetched_at": fetched_at,
    }


def _warn(msg: str) -> None:
    """경고 출력. GitHub Actions에서는 주석 문법으로 올려 요약 화면에 뜨게 한다."""
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"[경고] {msg}")


def last_run() -> dict[str, Any]:
    """마지막 성공 run의 메타데이터 (크레딧 미소모).

    데이터셋과 별개로 이걸 읽는 이유: 파이프라인은 항상 "마지막 성공 run"을 보므로,
    task가 멈춰도(크레딧 소진·액터 오류·스케줄 해제) 조용히 성공하며 옛 데이터를
    최신인 것처럼 리포트에 싣는다. finishedAt으로 나이를 재서 그 침묵을 깬다.
    """
    url = f"{API_BASE}/actor-tasks/{_task_id()}/runs/last"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, params={"status": "SUCCEEDED"}, headers=_headers())
        if resp.status_code == 404:
            raise SystemExit(
                f"Apify task '{_task_id()}'의 성공한 run이 없습니다 — 콘솔에서 한 번 실행하세요"
            )
        if resp.status_code in (401, 403):
            raise SystemExit(f"Apify 인증 실패({resp.status_code}) — APIFY_TOKEN 확인")
        resp.raise_for_status()
        d = resp.json().get("data") or {}
    return {
        "run_id": d.get("id"),
        "started_at": d.get("startedAt"),
        "finished_at": d.get("finishedAt"),
        "dataset_id": d.get("defaultDatasetId"),
    }


def run_age_hours(finished_at: str | None) -> float | None:
    """run 종료 시점부터 지금까지의 시간. 파싱 실패 시 None(판정 보류)."""
    if not finished_at:
        return None
    try:
        ts = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def check_freshness(meta: dict[str, Any]) -> float | None:
    """run 나이를 출력하고, 경고 임계를 넘으면 경고 / 중단 임계를 넘으면 SystemExit.

    중단(SKIP)을 택하는 이유: 옛 데이터로 시트를 다시 덮으면 updated_at만 갱신돼
    "방금 확인됐고 수치가 그대로"인 것과 구분되지 않는다. 아예 쓰지 않으면 시트의
    updated_at이 멈춘 시점에 머물러 문제가 눈에 보인다. cmd_collect가 SystemExit을
    SKIP(exit 0)으로 처리하므로 다른 소스 수집은 그대로 진행된다.
    """
    age = run_age_hours(meta.get("finished_at"))
    if age is None:
        _warn(f"Apify run의 종료 시각을 읽지 못했습니다 (finishedAt={meta.get('finished_at')!r})")
        return None
    print(f"[apify_instagram] 마지막 성공 run {meta.get('run_id')} — {age:.1f}시간 전 종료")
    if age >= APIFY_STALE_SKIP_HOURS:
        msg = (
            f"Apify 데이터가 {age / 24:.1f}일 묵었습니다 (임계 {APIFY_STALE_SKIP_HOURS:.0f}시간) "
            "— task가 멈춘 것으로 보고 시트 갱신을 건너뜁니다. Apify 콘솔에서 스케줄 활성 여부와 "
            "크레딧 잔량을 확인하세요"
        )
        # SystemExit은 cmd_collect에서 "키 미설정" 류의 SKIP과 같은 줄로 처리돼 눈에 안 띈다.
        # 이건 조치가 필요한 상황이므로 경고 주석을 따로 올린 뒤 중단한다.
        _warn(msg)
        raise SystemExit(msg)
    if age >= APIFY_STALE_WARN_HOURS:
        _warn(
            f"Apify 데이터가 {age:.1f}시간 전 것입니다 (임계 {APIFY_STALE_WARN_HOURS:.0f}시간) "
            "— task 실행이 지연됐거나 실패했을 수 있습니다"
        )
    return age


def fetch_last_dataset(limit: int | None = None) -> list[dict[str, Any]]:
    """마지막 성공 run의 데이터셋 원본 아이템 (크레딧 미소모)."""
    url = f"{API_BASE}/actor-tasks/{_task_id()}/runs/last/dataset/items"
    params = {"status": "SUCCEEDED", "clean": "true", "limit": limit or APIFY_DATASET_LIMIT}
    with httpx.Client(timeout=60) as client:
        resp = client.get(url, params=params, headers=_headers())
        if resp.status_code == 404:
            raise SystemExit(
                f"Apify task '{_task_id()}'의 성공한 run이 없습니다 — 콘솔에서 한 번 실행하세요"
            )
        if resp.status_code in (401, 403):
            raise SystemExit(f"Apify 인증 실패({resp.status_code}) — APIFY_TOKEN 확인")
        resp.raise_for_status()
        return resp.json()


USAGE_ALERT_STATE = "apify_usage_alerts"


def usage() -> dict[str, Any]:
    """계정의 이번 청구 주기 사용액과 한도. 계정 API라 크레딧을 소모하지 않는다.

    무료 플랜은 지출 한도(maxMonthlyUsageUsd)가 0으로 내려오는 경우가 있어, 그럴 때는
    플랜에 포함된 무료 크레딧(plan.monthlyUsageCreditsUsd)을 기준으로 삼는다.
    """
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{API_BASE}/users/me/limits", headers=_headers())
        if resp.status_code in (401, 403):
            raise SystemExit(f"Apify 인증 실패({resp.status_code}) — APIFY_TOKEN 확인")
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        used = (data.get("current") or {}).get("monthlyUsageUsd")
        limit = (data.get("limits") or {}).get("maxMonthlyUsageUsd")
        if not limit:
            me = client.get(f"{API_BASE}/users/me", headers=_headers())
            me.raise_for_status()
            plan = (me.json().get("data") or {}).get("plan") or {}
            limit = plan.get("monthlyUsageCreditsUsd")
        cycle = data.get("monthlyUsageCycle") or {}
    used = float(used) if used is not None else None
    limit = float(limit) if limit else None
    return {
        "used_usd": used,
        "limit_usd": limit,
        "ratio": (used / limit) if (used is not None and limit) else None,
        "cycle_start": cycle.get("startAt"),
        "cycle_end": cycle.get("endAt"),
    }


def check_usage_and_alert() -> int:
    """사용률이 임계값을 넘으면 이메일로 알린다. 주기·단계당 1회만 보낸다.

    수신자 미설정이면 조회 결과만 출력하고 끝낸다(알림은 선택 기능).
    """
    from ..config import APIFY_ALERT_EMAIL, APIFY_ALERT_THRESHOLDS
    from ..report import send_email

    u = usage()
    if u["ratio"] is None:
        print(f"[apify_usage] 사용액 {u['used_usd']} / 한도 {u['limit_usd']} — 비율 계산 불가")
        return 0
    pct = u["ratio"] * 100
    print(
        f"[apify_usage] ${u['used_usd']:.2f} / ${u['limit_usd']:.2f} ({pct:.1f}%)"
        f" — 주기 종료 {u['cycle_end']}"
    )
    if not APIFY_ALERT_EMAIL:
        return 0

    # 같은 주기에 이미 보낸 단계는 건너뛴다. 주기가 바뀌면 기록을 리셋한다.
    state = storage.load_state(USAGE_ALERT_STATE, {})
    if state.get("cycle_start") != u["cycle_start"]:
        state = {"cycle_start": u["cycle_start"], "sent": []}
    already = set(state.get("sent") or [])
    crossed = [t for t in APIFY_ALERT_THRESHOLDS if u["ratio"] >= t and str(t) not in already]
    if not crossed:
        return 0

    level = max(crossed)
    subject = f"[agape] Apify 크레딧 {pct:.0f}% 사용 — 임계 {level * 100:.0f}% 초과"
    body = "\n".join([
        f"# Apify 크레딧 경고 — {pct:.1f}% 사용",
        "",
        f"- 사용액: **${u['used_usd']:.2f}** / 한도 ${u['limit_usd']:.2f}",
        f"- 남은 금액: ${max(0.0, u['limit_usd'] - u['used_usd']):.2f}",
        f"- 청구 주기: {u['cycle_start']} ~ {u['cycle_end']}",
        "",
        "_주기가 초기화되면 다시 수집됩니다. 소진되면 파이프라인은 마지막 성공 run의 "
        "데이터를 계속 읽으므로 리포트는 동작하지만 인스타 데이터가 갱신되지 않습니다._",
    ])
    send_email(subject, body, [APIFY_ALERT_EMAIL])
    state["sent"] = sorted(already | {str(t) for t in crossed})
    storage.save_state(USAGE_ALERT_STATE, state)
    print(f"[apify_usage] 경고 메일 전송 (임계 {level * 100:.0f}%)")
    return 1


def collect() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    # 데이터를 받기 전에 나이를 본다 — 너무 묵었으면 여기서 SystemExit(SKIP)으로 끝낸다.
    meta = last_run()
    check_freshness(meta)
    raw = fetch_last_dataset()
    rows = [_norm(it, fetched_at) for it in raw if isinstance(it, dict)]
    rows = [r for r in rows if r.get("url")]  # URL 없는 행은 리포트에 못 쓴다
    # run 출처를 행에 심어 시트까지 흘려보낸다 — 시트만 봐도 어느 run의 수치인지 알 수 있다.
    for r in rows:
        r["run_id"] = meta.get("run_id")
        r["run_finished_at"] = meta.get("finished_at")
    if not rows:
        # 빈 파일을 만들면 DuckDB가 컬럼을 못 잡아 집계가 죽는다 — 아예 쓰지 않는다.
        # 액터 필드명이 _norm과 안 맞아 전부 걸러진 경우도 여기로 온다(apify-peek로 확인).
        print(f"[apify_instagram] 사용할 게시물이 없습니다 (원본 {len(raw)}건) — 저장 생략")
        return 0
    storage.write_jsonl("instagram_posts", rows)
    reels = sum(1 for r in rows if r["is_reel"])
    print(f"[apify_instagram] {len(rows)} posts (릴스 {reels}) — 마지막 성공 run 기준")
    return len(rows)
