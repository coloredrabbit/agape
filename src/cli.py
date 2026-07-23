from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import NaverFilters, load_channels_config, load_keyword_config
from . import metrics as metrics_mod
from . import report as report_mod


def cmd_collect(_: argparse.Namespace) -> int:
    from .collectors import naver, pinterest, pinterest_official, youtube

    cfg = load_keyword_config()
    sources = [
        ("naver_search", lambda: naver.collect_search_trend(cfg)),
        ("naver_shopping", lambda: naver.collect_shopping(cfg)),
        ("youtube_snapshot", youtube.snapshot),
        ("youtube_trending", youtube.trending),
        ("pinterest", lambda: pinterest.collect(cfg)),
        ("pinterest_official", pinterest_official.collect),
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
    result = metrics_mod.compute(
        [k.name for k in cfg.keywords],
        filters_tag=cfg.filters.tag,
        categories={k.name: k.category for k in cfg.keywords},
    )
    text = report_mod.render_markdown(result)
    if not args.quiet:
        print(text)
    if args.html:
        path = Path(args.html)
        path.parent.mkdir(parents=True, exist_ok=True)
        gen = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        title = f"헤어 트렌드 리포트 — {result['week']} 주"
        path.write_text(report_mod.html_document(text, title, gen, result), encoding="utf-8")
        print(f"[html] {path} 작성")
    if args.send:
        channels = load_channels_config()
        subject = f"[agape] 헤어 트렌드 리포트 — {result['week']} 주"
        only = {x.strip() for x in args.only.split(",")} if args.only else None
        return report_mod.deliver(text, subject, channels, only)
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
