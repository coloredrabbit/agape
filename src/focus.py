"""스타일 심층 분석 — 키워드 하나를 여러 각도로 파고드는 대시보드.

주간 리포트가 "키워드 수십 개를 넓게 훑는" 것이라면, 여기는 "밀 스타일 하나를 깊게" 본다.
표기·타겟 편향·시기·내 콘텐츠 성과를 한 화면에서 확인하는 용도이며, 데이터를 저장하지 않는
애드혹 분석이다(agape query와 같은 성격).

## 데이터랩 연령/성별 해석의 함정 (이 모듈 설계의 핵심)

데이터랩 ratio는 "요청 내 최대=100"인 상대값이라 필터가 다른 요청끼리 그대로 비교할 수 없다.
그래서 모든 요청에 앵커를 끼워 "앵커 대비 비율"로 환산하지만, **그것만으로는 부족하다** —
앵커 자체가 연령 편향을 갖기 때문이다. 실측(2026-08, 앵커=미용실):

    새치염색      10대 0.10 → 50대 1.55
    히피펌        10대 4.78 → 50대 8.37   ← 트렌드 펌인데도 고연령에서 최고
    레이어드C컬펌  10대 1.03 → 50대 3.95

세 키워드가 모두 나이순으로 증가했다. 젊은 층이 "미용실"이라는 일반어를 훨씬 많이 검색해
분모가 작아지는 고연령 쪽이 일률적으로 부풀려진 것이다. 따라서 앵커 대비 값의 절대 크기로
"이 연령대가 가장 많이 검색한다"고 말하면 안 된다.

대신 **기준 키워드와의 상대 비교**를 쓴다. 같은 앵커·같은 연령 필터로 얻은 두 값을 나누면
앵커가 상쇄되어(V_A/V_anchor ÷ V_B/V_anchor = V_A/V_B) 순수한 키워드 간 비율이 남는다.
연령 편향이 명확한 기준 키워드를 양 끝에 두고 그 사이 어디에 있는지 보면 편향 방향을
판정할 수 있다. 데이터랩은 절대 검색량을 제공하지 않으므로 "어느 연령이 절대적으로 많이
검색하는가"는 이 API로 answer할 수 없다 — 그 한계를 리포트에 명시한다.
"""
from __future__ import annotations

import statistics as st
from datetime import date, timedelta
from typing import Any

from .config import NaverFilters

AGES = ("10", "20", "30", "40", "50")
LOOKBACK_DAYS = 365
SEASON_DAYS = 730

# 연령 편향 판정용 기준 키워드 — 양 끝을 잡아 대상 키워드가 그 사이 어디에 있는지 본다.
# young: 트렌드 추종 스타일 / old: 고연령이 확실한 시술
REF_YOUNG = "히피펌"
REF_OLD = "새치염색"
SKEW_LABELS = (
    (0.30, "젊은 층 쏠림"),
    (0.55, "20~30대 중심"),
    (0.80, "30~40대 중심"),
    (1.00, "고연령 쏠림"),
)


def _mean_ratio(term: str, anchor: str, start: date, end: date,
                filters: NaverFilters) -> float | None:
    """앵커 대비 검색 강도(%). 필터가 다른 요청 간 비교를 가능하게 하는 정규화값."""
    from .collectors import naver

    series = naver.query([term, anchor], start, end, "month", filters)["series"]
    t = list(series.get(term, {}).values())
    a = list(series.get(anchor, {}).values())
    if not t or not a or st.mean(a) == 0:
        return None
    return st.mean(t) / st.mean(a) * 100


def compare_variants(variants: list[str], start: date, end: date) -> list[dict[str, Any]]:
    """표기 변형 비교 — 한 요청에 넣으므로 값끼리 직접 비교된다(앵커 불필요)."""
    from .collectors import naver

    series = naver.query(variants[:5], start, end, "month", NaverFilters())["series"]
    out = []
    for v in variants[:5]:
        pts = series.get(v, {})
        vals = [pts[p] for p in sorted(pts)]
        out.append({
            "term": v,
            "avg": st.mean(vals) if vals else 0.0,
            "recent": vals[-1] if vals else 0.0,
            "peak": max(pts, key=pts.get) if pts else None,
        })
    return sorted(out, key=lambda x: -x["avg"])


def _skew(low: float | None, high: float | None) -> float | None:
    """10대→50대 증가 배수. 클수록 고연령 쪽으로 기울어 있다."""
    if not low or not high or low <= 0:
        return None
    return high / low


def _skew_label(rel: float | None) -> str:
    """대상 키워드의 편향 위치를 기준 키워드 사이에서 라벨링 (0=젊음, 1=고연령)."""
    if rel is None:
        return "판정 불가"
    for cut, label in SKEW_LABELS:
        if rel <= cut:
            return label
    return "고연령 쏠림"


def demographics(term: str, anchor: str, start: date, end: date) -> dict[str, Any]:
    """연령·성별 편향 분석.

    앵커 대비 값(raw)은 참고로만 싣고, 판정은 기준 키워드와의 상대 비교로 한다 —
    앵커 자체의 연령 편향 때문에 raw 값의 절대 크기로는 판정할 수 없다(모듈 docstring 참고).

    요청 수 = 2(성별) + 연령수 × 3(대상 + 기준 2개).
    """
    gender = {
        label: _mean_ratio(term, anchor, start, end, NaverFilters(gender=g))
        for label, g in (("여성", "f"), ("남성", "m"))
    }
    terms = {"target": term, "young": REF_YOUNG, "old": REF_OLD}
    raw: dict[str, dict[str, float | None]] = {k: {} for k in terms}
    for age in AGES:
        f = NaverFilters(ages=(age,))
        for key, kw in terms.items():
            raw[key][age] = _mean_ratio(kw, anchor, start, end, f)

    # 같은 앵커·같은 연령이므로 나누면 앵커가 상쇄된다 → 순수 키워드 간 비율
    vs_young = {
        a: (raw["target"][a] / raw["young"][a])
        if raw["target"].get(a) and raw["young"].get(a) else None
        for a in AGES
    }
    skews = {k: _skew(raw[k].get(AGES[0]), raw[k].get(AGES[-1])) for k in terms}
    # 대상의 편향을 기준 두 개 사이 0~1로 환산 (로그 스케일 — 배수 비교이므로)
    rel = None
    sy, so, stg = skews["young"], skews["old"], skews["target"]
    if sy and so and stg and so > sy:
        import math
        rel = (math.log(stg) - math.log(sy)) / (math.log(so) - math.log(sy))
        rel = max(0.0, min(1.0, rel))
    return {
        "gender": gender,
        "raw": raw,
        "vs_young": vs_young,
        "skews": skews,
        "relative": rel,
        "verdict": _skew_label(rel),
        "ref_young": REF_YOUNG,
        "ref_old": REF_OLD,
    }


def seasonality(term: str, end: date) -> dict[str, Any]:
    """2년치 월별 추이에서 피크/저점 월을 찾는다."""
    from .collectors import naver

    start = end - timedelta(days=SEASON_DAYS)
    pts = naver.query([term], start, end, "month", NaverFilters())["series"].get(term, {})
    if not pts:
        return {"months": {}, "peak": None, "low": None}
    by_month: dict[int, list[float]] = {}
    for period, v in pts.items():
        by_month.setdefault(int(period[5:7]), []).append(v)
    avg = {m: st.mean(vs) for m, vs in by_month.items()}
    return {
        "months": avg,
        "peak": max(avg, key=avg.get) if avg else None,
        "low": min(avg, key=avg.get) if avg else None,
    }


def _norm_tag(s: str) -> str:
    return "".join(s.split()).lower()


def my_content(variants: list[str]) -> list[dict[str, Any]]:
    """내 인스타 게시물 중 해당 스타일 태그가 붙은 것들의 성과 (수집돼 있을 때만)."""
    import duckdb

    from . import storage

    if not storage.has_data("instagram_posts"):
        return []
    con = duckdb.connect()
    rows = con.execute(
        """
        SELECT tag, count(*) AS n, median(views)::BIGINT AS med_views,
               median(TRY_CAST(likes AS BIGINT) * 10000.0
                      / nullif(TRY_CAST(views AS BIGINT), 0)) AS eng
        FROM (
            SELECT unnest(hashtags) AS tag, views, likes
            FROM (
                SELECT hashtags, views, likes,
                       ROW_NUMBER() OVER (PARTITION BY url ORDER BY fetched_at DESC) AS rn
                FROM read_ndjson_auto(?, union_by_name=true, sample_size=-1)
                WHERE NOT coalesce(is_pinned, false)
            ) WHERE rn = 1
        )
        GROUP BY tag
        """,
        [storage.source_glob("instagram_posts")],
    ).fetchall()
    wanted = {_norm_tag(v) for v in variants}
    return sorted(
        ({"tag": t, "n": n, "med_views": mv, "engagement": eng}
         for t, n, mv, eng in rows if _norm_tag(t) in wanted),
        key=lambda x: -x["n"],
    )


def all_my_tags(limit: int = 8) -> list[dict[str, Any]]:
    """내가 많이 쓰는 태그 상위 N — 검색 수요와의 격차를 보기 위한 목록."""
    import duckdb

    from . import storage

    if not storage.has_data("instagram_posts"):
        return []
    con = duckdb.connect()
    rows = con.execute(
        """
        SELECT tag, count(*) AS n FROM (
            SELECT unnest(hashtags) AS tag FROM (
                SELECT hashtags, ROW_NUMBER() OVER (PARTITION BY url ORDER BY fetched_at DESC) AS rn
                FROM read_ndjson_auto(?, union_by_name=true, sample_size=-1)
                WHERE NOT coalesce(is_pinned, false)
            ) WHERE rn = 1
        ) GROUP BY tag ORDER BY n DESC LIMIT ?
        """,
        [storage.source_glob("instagram_posts"), int(limit)],
    ).fetchall()
    return [{"tag": t, "n": n} for t, n in rows]


def analyze(term: str, variants: list[str], anchor: str = "미용실") -> dict[str, Any]:
    """심층 분석 실행. 데이터랩 요청 수 ≈ 1(표기) + 2(성별) + 15(연령×3키워드) + 1(계절성) = 약 19회."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=LOOKBACK_DAYS)
    terms = [term] + [v for v in variants if v != term]
    return {
        "term": term,
        "variants": compare_variants(terms, start, end),
        "demographics": demographics(term, anchor, start, end),
        "seasonality": seasonality(term, end),
        "my_content": my_content(terms),
        "my_top_tags": all_my_tags(),
        "anchor": anchor,
        "period": (start, end),
    }


def render(result: dict[str, Any]) -> str:
    """분석 결과를 마크다운 대시보드로."""
    term = result["term"]
    start, end = result["period"]
    L = [f"# 스타일 심층 분석 — {term}", "",
         f"_기간 {start} ~ {end} · 앵커 «{result['anchor']}» 정규화 · 연령 판정은 기준 키워드 대비_"]

    # 1. 표기 비교
    L += ["", "## 1. 표기 비교 — 소비자가 실제로 검색하는 말", "",
          "| 표기 | 평균 | 최근 | 최고점 |", "|---|---|---|---|"]
    for v in result["variants"]:
        peak = v["peak"] or "-"
        L.append(f"| {v['term']} | {v['avg']:.1f} | {v['recent']:.1f} | {peak} |")
    top = result["variants"][0] if result["variants"] else None
    dead = [v["term"] for v in result["variants"] if v["avg"] < 0.5]
    if top:
        L += ["", f"→ **검색 1위 표기: {top['term']}** — 플레이스 메뉴명·캡션에 이 표기를 그대로 쓰세요."]
    if dead:
        L.append(f"→ ⚠️ 검색량이 사실상 없는 표기: {', '.join(dead)} — 네이버 노출 목적으로는 무의미합니다.")

    # 2. 데모그래픽 — 편향 판정(기준 키워드 대비)만 하고 절대 순위는 말하지 않는다
    d = result["demographics"]
    L += ["", "## 2. 타겟 — 연령 편향", ""]

    gv = [(k, v) for k, v in d["gender"].items() if v is not None]
    if gv and min(v for _, v in gv) > 0:
        hi, lo = max(gv, key=lambda x: x[1]), min(gv, key=lambda x: x[1])
        L.append(f"- 성별: **{hi[0]} 우세** ({hi[0]} {hi[1]:.2f} vs {lo[0]} {lo[1]:.2f}, "
                 f"{hi[1] / lo[1]:.0f}배)")

    sk, ry, ro = d["skews"], d["ref_young"], d["ref_old"]
    if sk.get("target"):
        L += ["", "10대→50대 증가 배수 (클수록 고연령 쪽):", "",
              "| 키워드 | 배수 | 역할 |", "|---|---|---|"]
        for key, label in (("young", f"{ry} (기준: 젊은 층)"),
                           ("target", f"**{term}**"),
                           ("old", f"{ro} (기준: 고연령)")):
            v = sk.get(key)
            L.append(f"| {label} | {v:.1f}배 |" + (" 대상 |" if key == "target" else " 대조군 |")
                     if v else f"| {label} | - | - |")
        L += ["", f"→ **판정: {d['verdict']}**"]
        if d["relative"] is not None:
            L.append(f"  (기준 두 개 사이 위치 {d['relative']:.0%} — 0%={ry}만큼 젊고, "
                     f"100%={ro}만큼 고연령)")

    vy = [(a, v) for a, v in d["vs_young"].items() if v is not None]
    if vy:
        L += ["", f"연령별 «{term} ÷ {ry}» 비율 (앵커가 상쇄된 순수 비교):", "",
              "| 연령 | 비율 |", "|---|---|"]
        for a, v in vy:
            L.append(f"| {a}대 | {v:.2f} |")

    L += ["", "> ⚠️ **해석 주의**: 데이터랩은 절대 검색량을 제공하지 않으므로 "
          "\"어느 연령이 가장 많이 검색하는가\"는 이 데이터로 알 수 없습니다. "
          "위 판정은 **기준 키워드 대비 상대적 기울기**입니다. "
          f"앵커(«{result['anchor']}») 자체에 연령 편향이 있어 앵커 대비 값의 절대 크기로 "
          "순위를 매기면 트렌드 스타일도 고연령 1위로 나오는 오류가 생깁니다.\n"
          "> 실제 도달 연령대는 **인스타 프로페셔널 계정 인사이트**(심사 불필요)로 확인하는 게 "
          "가장 직접적입니다."]

    # 3. 계절성
    s = result["seasonality"]
    if s["peak"]:
        L += ["", "## 3. 시기 — 언제 밀어야 하나", "",
              f"- 최고 수요: **{s['peak']}월** · 최저: {s['low']}월",
              f"- → {s['peak']}월 **1~2개월 전**부터 콘텐츠와 플레이스 메뉴를 준비하세요."]

    # 4. 내 콘텐츠 성과
    mine = result["my_content"]
    if mine:
        L += ["", "## 4. 내 인스타 성과 (해당 스타일 태그)", "",
              "| 태그 | 게시물 | 조회 중앙값 | 참여율(‱) |", "|---|---|---|---|"]
        for m in mine:
            eng = f"{m['engagement']:.0f}" if m["engagement"] is not None else "-"
            L.append(f"| #{m['tag']} | {m['n']} | {m['med_views']:,} | {eng} |")

    # 5. 갭 분석 — 내가 쓰는 태그 vs 검색 수요
    tags, variants = result["my_top_tags"], result["variants"]
    if tags and variants:
        vol = {_norm_tag(v["term"]): v["avg"] for v in variants}
        gaps = [(t["tag"], t["n"], vol[_norm_tag(t["tag"])])
                for t in tags if _norm_tag(t["tag"]) in vol]
        if gaps:
            L += ["", "## 5. 갭 — 내 태그 사용량 vs 검색 수요", "",
                  "| 태그 | 내 사용 | 검색 강도 | 판정 |", "|---|---|---|---|"]
            for tag, n, v in sorted(gaps, key=lambda x: -x[1]):
                verdict = ("⚠️ 검색 수요 없음" if v < 0.5
                           else "✅ 수요 높음" if v >= 20 else "보통")
                L.append(f"| #{tag} | {n}건 | {v:.1f} | {verdict} |")
            L.append("\n_인스타 해시태그 도달과 네이버 검색은 별개입니다 — 위 판정은 "
                     "**플레이스 메뉴명·캡션 표기**를 정할 때 쓰세요._")

    return "\n".join(L)
