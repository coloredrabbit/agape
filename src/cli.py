from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import NaverFilters, load_channels_config, load_keyword_config
from . import metrics as metrics_mod
from . import report as report_mod


def cmd_collect(_: argparse.Namespace) -> int:
    from .collectors import apify, naver, pinterest, pinterest_official, youtube

    cfg = load_keyword_config()
    sources = [
        ("naver_search", lambda: naver.collect_search_trend(cfg)),
        ("naver_shopping", lambda: naver.collect_shopping(cfg)),
        ("youtube_snapshot", youtube.snapshot),
        ("youtube_trending", youtube.trending),
        ("pinterest", lambda: pinterest.collect(cfg)),
        ("pinterest_official", pinterest_official.collect),
        # 마지막 성공 run만 읽는다 — 크레딧 미소모
        ("apify_instagram", apify.collect),
    ]
    skipped, errored = [], []
    for name, fn in sources:
        try:
            fn()
        except SystemExit as e:
            # 키 미설정 등 의도된 생략 — 선택 소스가 있으므로 실패로 치지 않는다
            print(f"[{name}] SKIP: {e}", file=sys.stderr)
            skipped.append(name)
        except Exception as e:  # noqa: BLE001 — 한 소스 실패가 나머지 수집을 막지 않게
            print(f"[{name}] ERROR: {e}", file=sys.stderr)
            errored.append(name)
    ok = len(sources) - len(skipped) - len(errored)
    print(f"[collect] 성공 {ok} · 스킵 {len(skipped)} · 실패 {len(errored)}")
    if errored:
        return 1
    if ok == 0:
        print("[collect] 수집된 소스가 하나도 없습니다 — 키 설정을 확인하세요", file=sys.stderr)
        return 1
    return 0


def cmd_discover(_: argparse.Namespace) -> int:
    from .collectors import youtube

    cfg = load_keyword_config()
    youtube.discover(cfg)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_keyword_config()
    # 리포트의 지표 축은 네이버 시계열이므로, 해외 발굴용 키워드는 집계 대상에서 뺀다
    # (넣으면 "데이터 확보 N개" 커버리지가 실제보다 나쁘게 보인다).
    result = metrics_mod.compute(
        [k.name for k in cfg.naver_keywords],
        filters_tag=cfg.filters.tag,
        categories={k.name: k.category for k in cfg.keywords},
    )
    # 시트가 설정돼 있으면 인스타 섹션은 시트를 소스로 쓴다 — 원본 JSONL은 커밋되지 않으므로
    # CI에서는 시트가 유일한 영속 저장소이고, view 수도 시트 쪽이 최신(매일 upsert)이다.
    from .config import GSHEET_WEBHOOK_URL

    if GSHEET_WEBHOOK_URL:
        try:
            from . import gsheet

            result["instagram_reels"] = _reels_from_sheet(gsheet.fetch_rows("instagram"))
        except Exception as e:  # noqa: BLE001 — 시트 장애가 리포트 전체를 막지 않게
            print(f"[gsheet] 시트 조회 실패 — 로컬 데이터로 대체: {e}", file=sys.stderr)

    text = report_mod.render_markdown(result)
    if not args.quiet:
        print(text)
    if args.html:
        path = Path(args.html)
        path.parent.mkdir(parents=True, exist_ok=True)
        gen = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        title = f"헤어 트렌드 리포트 — {result['week']} 주"
        # HTML은 릴스/영상을 자체 섹션으로 다시 그리므로 마크다운 쪽에서는 뺀다(중복 방지)
        web_md = report_mod.render_markdown(result, omit_rich=True)
        path.write_text(report_mod.html_document(web_md, title, gen, result), encoding="utf-8")
        print(f"[html] {path} 작성")
    if args.send:
        channels = load_channels_config()
        subject = f"[agape] 헤어 트렌드 리포트 — {result['week']} 주"
        only = {x.strip() for x in args.only.split(",")} if args.only else None
        return report_mod.deliver(text, subject, channels, only)
    return 0


def cmd_apify_peek(args: argparse.Namespace) -> int:
    """액터 출력 필드를 눈으로 확인 (저장 안 함) — 필드 매핑을 맞출 때 사용."""
    import json

    from .collectors import apify

    raw = apify.fetch_last_dataset(limit=args.limit)
    if not raw:
        print("데이터셋이 비어 있습니다 — Apify 콘솔에서 task를 한 번 실행하세요")
        return 1
    keys: dict[str, int] = {}
    for item in raw:
        if isinstance(item, dict):
            for k in item:
                keys[k] = keys.get(k, 0) + 1
    print(f"# Apify 데이터셋 {len(raw)}건 — 필드 출현 빈도")
    for k, n in sorted(keys.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<28} {n}/{len(raw)}")
    print("\n# 첫 번째 아이템 원본")
    print(json.dumps(raw[0], ensure_ascii=False, indent=2)[:2000])
    print("\n# 정규화 결과")
    from datetime import datetime as _dt

    print(json.dumps(apify._norm(raw[0], _dt.now(timezone.utc).isoformat()),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_apify_usage(_: argparse.Namespace) -> int:
    """Apify 크레딧 사용률 확인 + 임계 초과 시 이메일 경고 (계정 API — 크레딧 미소모)."""
    from .collectors import apify

    apify.check_usage_and_alert()
    return 0


# run_finished_at(Apify가 실제로 긁은 시각)과 updated_at(우리가 시트에 쓴 시각)을 나란히
# 둔다 — 둘의 간격이 벌어지면 "매일 잘 돌고 있는데 수치가 안 변한다"가 아니라 "task가 멈췄다"
# 임을 시트만 보고 알 수 있다.
SHEET_HEADER = ["key", "link", "title", "content", "hashtags", "views", "view_count",
                "likes", "comments", "duration_sec", "music", "is_pinned", "owner",
                "posted_at", "run_finished_at", "updated_at"]
CONTENT_MAX = 5000  # 시트 셀 부담을 줄이기 위한 본문 상한 (셀 한도 5만자보다 훨씬 아래)


def cmd_export_sheet(args: argparse.Namespace) -> int:
    """수집된 인스타 게시물을 Google Sheets에 upsert.

    key(게시물 ID) 기준으로 이미 있는 행은 통째로 갱신한다 — view 수가 매일 바뀌므로
    append가 아니라 update가 맞다. 신규 게시물만 행이 추가되므로 중복이 쌓이지 않는다.
    제목은 캡션 첫 줄, 내용은 전체 캡션으로 매핑한다(인스타에 별도 제목 개념이 없음).
    """
    import duckdb

    from . import gsheet, storage

    if not storage.has_data("instagram_posts"):
        print("적재할 인스타 데이터가 없습니다 — 먼저 collect를 실행하세요")
        return 1
    con = duckdb.connect()
    glob = storage.source_glob("instagram_posts")
    # run_finished_at은 나중에 추가된 필드다. 모든 파일에 없으면 union_by_name도 컬럼을
    # 만들어주지 못해 SELECT가 BinderException으로 죽으므로, 존재를 확인하고 없으면 NULL로 채운다.
    present = {
        r[0]
        for r in con.execute(
            "DESCRIBE SELECT * FROM read_ndjson_auto(?, union_by_name=true, sample_size=-1)",
            [glob],
        ).fetchall()
    }
    run_col = "run_finished_at" if "run_finished_at" in present else "NULL AS run_finished_at"
    rows = con.execute(
        f"""
        SELECT post_id, shortcode, url, caption, hashtags, views, view_count,
               likes, comments, duration_sec, music, is_pinned, owner, posted_at,
               run_finished_at
        FROM (
            SELECT post_id, shortcode, url, caption, hashtags, views, view_count,
                   likes, comments, duration_sec, music, is_pinned, owner, posted_at,
                   {run_col},
                   ROW_NUMBER() OVER (PARTITION BY url ORDER BY fetched_at DESC) AS rn
            FROM read_ndjson_auto(?, union_by_name=true, sample_size=-1)
            WHERE url IS NOT NULL
        ) WHERE rn = 1
        ORDER BY coalesce(TRY_CAST(views AS BIGINT), 0) DESC
        """,
        [glob],
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for (post_id, shortcode, url, caption, hashtags, views, view_count,
         likes, comments, duration_sec, music, is_pinned, owner, posted_at,
         run_finished_at) in rows:
        caption = caption or ""
        title = " ".join(caption.split("\n")[0].split())[:80]
        tags = ", ".join(str(t) for t in (hashtags or []))
        cells = [
            str(post_id or shortcode or url),   # key — 안정적 식별자 우선
            url,
            title,
            caption[:CONTENT_MAX],
            tags,
            views if views is not None else "",
            view_count if view_count is not None else "",
            likes if likes is not None else "",
            comments if comments is not None else "",
            round(float(duration_sec), 1) if duration_sec is not None else "",
            music or "",
            "Y" if is_pinned else "",
            owner or "",
            posted_at or "",
            run_finished_at or "",
            now,
        ]
        payload.append([gsheet.sheet_safe(v) for v in cells])

    gsheet.upsert_rows(args.sheet, SHEET_HEADER, payload)
    return 0


def _int_or_none(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _reels_from_sheet(rows: list[dict]) -> list[dict]:
    """시트 행 → 리포트 릴스 목록 (view 수 내림차순 톱 N).

    시트가 인스타 데이터의 영속 저장소다(원본 JSONL은 개인정보라 커밋하지 않으므로) —
    리포트는 로컬 스냅샷 대신 시트의 최신 view 수를 쓴다. URL 화이트리스트 검증은
    렌더링 단계(report의 _visible_reels)가 동일하게 적용한다.
    고정 게시물은 노출이 누적돼 뷰가 비정상적으로 높으므로 순위에서 제외한다.
    """
    from .metrics import INSTAGRAM_TOP_REELS

    items = []
    for r in rows:
        link = str(r.get("link") or "").strip()
        if not link:
            continue
        if str(r.get("is_pinned") or "").strip().upper() in ("Y", "TRUE", "1"):
            continue
        items.append({
            "url": link,
            "caption": str(r.get("content") or r.get("title") or ""),
            "owner": str(r.get("owner") or "") or None,
            "views": _int_or_none(r.get("views")),
            "likes": _int_or_none(r.get("likes")),
            "comments": _int_or_none(r.get("comments")),
            "posted_at": r.get("posted_at") or None,
        })
    items.sort(key=lambda x: (x["views"] is None, -(x["views"] or 0), -(x["likes"] or 0)))
    top = items[:INSTAGRAM_TOP_REELS]
    for i, it in enumerate(top, start=1):
        it["rank"] = i
    return top


def cmd_sheet_show(args: argparse.Namespace) -> int:
    """시트에 적재된 인스타 데이터를 화면(표준출력)에 표시 — CI 로그 확인용."""
    from . import gsheet

    rows = gsheet.fetch_rows(args.sheet)
    if not rows:
        print("시트가 비어 있습니다 — collect + export-sheet 후 다시 확인하세요")
        return 0

    # 헤더가 코드의 기대와 다르면 값이 다른 열로 밀려 조용히 오답이 된다(실제로 겪음).
    # Apps Script v3가 헤더를 자동 동기화하지만, 구버전 배포를 쓰고 있으면 여기서 잡는다.
    missing = [c for c in ("key", "link", "views") if c not in rows[0]]
    if missing:
        print(
            f"⚠️  시트 헤더에 {missing} 열이 없습니다 — Apps Script를 최신 버전으로 "
            "재배포(배포 → 새 배포)한 뒤 export-sheet를 다시 실행하세요.\n"
            f"    현재 헤더: {[k for k in rows[0] if k]}",
            file=sys.stderr,
        )
        return 1

    reels = _reels_from_sheet(rows)
    print(f"\n# 시트 '{args.sheet}' — 전체 {len(rows)}건, view 상위 {len(reels)}건")
    print("순위 | views | 제목 | 링크")
    print("--- | --- | --- | ---")
    for it in reels:
        views = f"{it['views']:,}" if it["views"] is not None else "-"
        title = (it["caption"].splitlines() or [""])[0][:40]
        print(f"#{it['rank']} | {views} | {title} | {it['url']}")
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    """스타일 하나를 심층 분석 (저장 안 함). 데이터랩 요청 ~10회."""
    from . import focus

    variants = [v.strip() for v in (args.variants or "").split(",") if v.strip()]
    result = focus.analyze(args.term, variants, anchor=args.anchor)
    text = focus.render(result)
    print(text)
    if args.html:
        path = Path(args.html)
        path.parent.mkdir(parents=True, exist_ok=True)
        gen = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        path.write_text(
            report_mod.html_document(text, f"스타일 분석 — {args.term}", gen), encoding="utf-8"
        )
        print(f"\n[html] {path} 작성")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    from .collectors import naver

    try:
        filters = NaverFilters(
            device=args.device or "",
            gender=args.gender or "",
            ages=tuple(a.strip() for a in args.ages.split(",")) if args.ages else (),
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365)
    terms = [t.strip() for t in args.terms.split(",") if t.strip()]

    result = naver.query(terms, start, end, args.unit, filters)
    series = result["series"]

    cond = f"{start} ~ {end}, 단위={args.unit}, 필터={filters.tag}"
    print(f"# 네이버 검색 트렌드 조회 — {cond}")
    print("(같은 요청 내 상대값이라 키워드끼리 직접 비교 가능, 기간 내 최대=100)\n")

    periods = sorted({p for s in series.values() for p in s})
    shown = periods[-args.rows :] if len(periods) > args.rows else periods
    if len(periods) > len(shown):
        print(f"(전체 {len(periods)}개 구간 중 최근 {len(shown)}개만 표시 — --rows로 조정)\n")

    header = ["기간"] + terms
    print(" | ".join(header))
    print(" | ".join("---" for _ in header))
    for p in shown:
        cells = [p] + [
            f"{series.get(t, {}).get(p):.1f}" if series.get(t, {}).get(p) is not None else "-"
            for t in terms
        ]
        print(" | ".join(cells))

    print()
    for t in terms:
        s = series.get(t, {})
        if not s:
            print(f"- {t}: 데이터 없음 (검색량이 매우 적으면 미제공)")
            continue
        values = [s[p] for p in sorted(s)]
        peak = max(s, key=s.get)
        print(f"- {t}: 최근 {values[-1]:.1f}, 평균 {sum(values) / len(values):.1f}, 최고점 {peak}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="agape", description="SNS 기반 헤어 트렌드 감지 (저비용 MVP)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="일일 수집: 네이버 데이터랩 + 유튜브 스냅샷/인기차트")
    p_collect.set_defaults(fn=cmd_collect)

    p_discover = sub.add_parser("discover", help="주간: 유튜브 검색으로 추적 영상 풀 확장 (쿼터 소모 큼)")
    p_discover.set_defaults(fn=cmd_discover)

    p_report = sub.add_parser("report", help="주간 트렌드 리포트 출력/전송")
    p_report.add_argument("--send", action="store_true", help="channels.yaml의 enabled 채널로 전송")
    p_report.add_argument("--only", help="특정 채널 이름만 전송 (쉼표 구분, 예: me,team-slack)")
    p_report.add_argument("--html", help="리포트를 HTML 파일로 저장 (예: public/index.html — GitHub Pages용)")
    p_report.add_argument("--quiet", action="store_true", help="표준출력 생략")
    p_report.set_defaults(fn=cmd_report)

    p_peek = sub.add_parser(
        "apify-peek", help="Apify 데이터셋의 실제 필드 확인 (저장 안 함, 크레딧 미소모)"
    )
    p_peek.add_argument("--limit", type=int, default=5, help="조회할 아이템 수 (기본 5)")
    p_peek.set_defaults(fn=cmd_apify_peek)

    p_usage = sub.add_parser(
        "apify-usage", help="Apify 크레딧 사용률 확인 · 임계 초과 시 메일 경고 (크레딧 미소모)"
    )
    p_usage.set_defaults(fn=cmd_apify_usage)

    p_sheet = sub.add_parser(
        "export-sheet", help="인스타 데이터를 Google Sheets에 upsert (key 기준 갱신)"
    )
    p_sheet.add_argument("--sheet", default="instagram", help="시트 탭 이름 (기본 instagram)")
    p_sheet.set_defaults(fn=cmd_export_sheet)

    p_show = sub.add_parser("sheet-show", help="시트의 인스타 데이터를 조회해 표시 (CI 로그용)")
    p_show.add_argument("--sheet", default="instagram", help="시트 탭 이름 (기본 instagram)")
    p_show.set_defaults(fn=cmd_sheet_show)

    p_focus = sub.add_parser(
        "focus",
        help="스타일 1개 심층 분석: 표기 비교·연령/성별 타겟·계절성·내 콘텐츠 성과 (저장 안 함)",
    )
    p_focus.add_argument("term", help="분석할 스타일 (예: 레이어드C컬펌)")
    p_focus.add_argument(
        "--variants", help="비교할 표기 변형, 쉼표 구분 (예: 레이어드컷,C컬펌,볼륨레이어드)"
    )
    p_focus.add_argument("--anchor", default="미용실", help="정규화 기준 키워드 (기본 미용실)")
    p_focus.add_argument("--html", help="결과를 HTML로 저장 (예: public/focus.html)")
    p_focus.set_defaults(fn=cmd_focus)

    p_query = sub.add_parser(
        "query",
        help="네이버 검색 트렌드 애드혹 조회 (저장 안 함) — 기간/기기/성별/연령 필터 지원",
    )
    p_query.add_argument("terms", help="키워드 1~5개, 쉼표 구분 (예: 허쉬컷,히피펌)")
    p_query.add_argument("--start", help="시작일 YYYY-MM-DD (기본: 종료일-365일, 최소 2016-01-01)")
    p_query.add_argument("--end", help="종료일 YYYY-MM-DD (기본: 어제)")
    p_query.add_argument("--unit", choices=["date", "week", "month"], default="week", help="집계 단위 (기본 week)")
    p_query.add_argument("--device", choices=["pc", "mo"], help="기기 범위 (기본 전체)")
    p_query.add_argument("--gender", choices=["f", "m"], help="성별 (기본 전체)")
    p_query.add_argument("--ages", help="연령대, 쉼표 구분: 10~60 (예: 20,30)")
    p_query.add_argument("--rows", type=int, default=30, help="표에 표시할 최근 구간 수 (기본 30)")
    p_query.set_defaults(fn=cmd_query)

    args = parser.parse_args()
    sys.exit(args.fn(args))
