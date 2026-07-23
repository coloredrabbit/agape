from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from .config import (
    SLACK_WEBHOOK_URL,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_STARTTLS,
    SMTP_USER,
    Channel,
)
from .metrics import HISTORY_WEEKS, KeywordMetrics

TOP_N = 15
MOVERS_N = 10       # "네이버 상승 톱" 표 행 수
CHARTS_N = 6        # HTML 리포트에 넣을 추이 차트 수
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:+.0f}%" if v is not None else "-"


def _fmt_num(v: float | None, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if v is not None else "-"


def _fmt_int(v: int | None) -> str:
    return f"{v:,}" if v is not None else "-"


def _sparkline(points: list[dict[str, Any]]) -> str:
    """주간 시계열을 유니코드 블록 문자로 압축한 미니 추이.

    텍스트라서 터미널·Slack·이메일 어디서든 그대로 보인다. 값이 2개 미만이면 "-".
    """
    values = [p["vol"] for p in points if p.get("vol") is not None]
    if len(values) < 2:
        return "-"
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return SPARK_CHARS[3] * len(values)
    span = hi - lo
    return "".join(SPARK_CHARS[min(7, int((v - lo) / span * 8))] for v in values)


def _signal_summary(metrics: list[KeywordMetrics]) -> str:
    """리포트 최상단 한 줄 요약 — 시그널 분포, 등락 폭, 소스별 커버리지."""
    counts = {"강한 후보": 0, "관찰": 0, "계절성 의심": 0}
    for m in metrics:
        if m.signal:
            counts[m.signal] += 1
    ups = sum(1 for m in metrics if m.naver_velocity is not None and m.naver_velocity > 0)
    downs = sum(1 for m in metrics if m.naver_velocity is not None and m.naver_velocity < 0)
    naver_n = sum(1 for m in metrics if m.naver_vol is not None)
    yt_n = sum(1 for m in metrics if m.yt_views is not None)
    pin_n = sum(1 for m in metrics if m.pin_mom is not None)
    sig = " · ".join(f"{name} {n}" for name, n in counts.items())
    return (
        f"**시그널** {sig}  |  **네이버 WoW** ↑{ups} ↓{downs}  |  "
        f"**커버리지** 네이버 {naver_n} · 유튜브 {yt_n} · 핀터레스트 {pin_n} (추적 {len(metrics)}개)"
    )


def render_markdown(result: dict[str, Any]) -> str:
    week = result["week"]
    metrics: list[KeywordMetrics] = result["metrics"]
    history: dict[str, list[dict[str, Any]]] = result.get("history") or {}
    lines = [f"# 헤어 트렌드 주간 리포트 — {week.isoformat()} 주"]

    lines.append("")
    lines.append(_signal_summary(metrics))

    flagged = [m for m in metrics if m.signal][:TOP_N]
    if flagged:
        lines.append("")
        lines.append(
            "| 시그널 | 키워드 | 네이버 z | 네이버 WoW | 전년비 | 유튜브 Δ뷰 | 유튜브 WoW | 핀 MoM | 추이 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for m in flagged:
            lines.append(
                f"| {m.signal} | {m.keyword} | {_fmt_num(m.naver_z)} | {_fmt_pct(m.naver_velocity)} "
                f"| {_fmt_pct(m.naver_yoy)} | {_fmt_int(m.yt_views)} | {_fmt_pct(m.yt_velocity)} "
                f"| {_fmt_pct(m.pin_mom)} | {_sparkline(history.get(m.keyword, []))} |"
            )
    else:
        lines.append("")
        lines.append("이번 주 급등 시그널 없음.")

    movers = sorted(
        (m for m in metrics if m.naver_velocity is not None),
        key=lambda m: m.naver_velocity,
        reverse=True,
    )[:MOVERS_N]
    if movers:
        lines.append("")
        lines.append(f"**네이버 검색 상승 톱 {len(movers)}**")
        lines.append("")
        lines.append("| 키워드 | 카테고리 | WoW | z | 시그널 | 추이 |")
        lines.append("|---|---|---|---|---|---|")
        for m in movers:
            lines.append(
                f"| {m.keyword} | {m.category or '-'} | {_fmt_pct(m.naver_velocity)} "
                f"| {_fmt_num(m.naver_z)} | {m.signal or '-'} | {_sparkline(history.get(m.keyword, []))} |"
            )

    by_cat: dict[str, list[KeywordMetrics]] = {}
    for m in metrics:
        if m.category and m.naver_velocity is not None:
            by_cat.setdefault(m.category, []).append(m)
    if by_cat:
        lines.append("")
        lines.append("**카테고리 동향 (네이버 WoW)**")
        lines.append("")
        lines.append("| 카테고리 | 키워드 수 | 평균 WoW | 상승/하락 | 최고 상승 |")
        lines.append("|---|---|---|---|---|")
        cat_avg = lambda ms: sum(m.naver_velocity for m in ms) / len(ms)  # noqa: E731
        for cat, ms in sorted(by_cat.items(), key=lambda kv: -cat_avg(kv[1])):
            ups = sum(1 for m in ms if m.naver_velocity > 0)
            downs = sum(1 for m in ms if m.naver_velocity < 0)
            top = max(ms, key=lambda m: m.naver_velocity)
            lines.append(
                f"| {cat} | {len(ms)} | {_fmt_pct(cat_avg(ms))} | {ups}↑ {downs}↓ "
                f"| {top.keyword} ({_fmt_pct(top.naver_velocity)}) |"
            )

    if result.get("pinterest_candidates"):
        lines.append("")
        lines.append("**핀터레스트 급상승 — 사전 미등록 헤어 후보 (keywords.yaml 추가 검토)**")
        for c in result["pinterest_candidates"]:
            lines.append(f"- #{c['rank']} {c['term']} (MoM {_fmt_pct(c['mom'])}, WoW {_fmt_pct(c['wow'])})")

    if result.get("pinterest_official"):
        topics = result["pinterest_official"]
        region = topics[0]["region"]
        lines.append("")
        lines.append(f"**핀터레스트 공식 — {region} 트렌딩 토픽 (글로벌 선행 신호)**")
        for t in topics[:8]:
            # percent_growth_mom은 이미 퍼센트 단위(예: 85 = +85%)라 ×100 하지 않는다
            g = t["pct_growth_mom"]
            lines.append(f"- {t['title']} (MoM {f'{g:+.0f}%' if g is not None else '-'})")

    if result.get("shopping") or result.get("shopping_keywords"):
        lines.append("")
        lines.append("**쇼핑 클릭 (네이버 쇼핑인사이트)**")
        for s in result.get("shopping", []):
            lines.append(f"- [카테고리] {s['category']}: WoW {_fmt_pct(s['velocity'])}")
        for s in result.get("shopping_keywords", [])[:8]:
            lines.append(f"- [제품] {s['keyword']}: WoW {_fmt_pct(s['velocity'])}")

    if result.get("trending"):
        lines.append("")
        lines.append("**유튜브 인기 급상승 중 헤어 관련**")
        for t in result["trending"][:5]:
            lines.append(f"- #{t['rank']} {t['title']} ({t['channel']}, {_fmt_int(t['views'])}뷰)")

    lines.append("")
    lines.append(
        "_z: 4주 이동평균 대비 편차(표준편차 단위) · WoW: 전주 대비 · 전년비: 전년 동기 대비 · "
        f"추이: 최근 {HISTORY_WEEKS}주 네이버 검색(앵커 보정) 스파크라인_"
    )
    return "\n".join(lines)


def send_slack(text: str, webhook_url: str | None = None) -> None:
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        raise SystemExit("Slack webhook URL이 없습니다 (.env의 SLACK_WEBHOOK_URL 또는 channels.yaml 확인)")
    resp = httpx.post(url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def _inline_html(s: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", s)
    return s


def _table_html(block: list[str]) -> str:
    rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in block]
    body = [r for r in rows if not all(re.fullmatch(r":?-{3,}:?", c or "") for c in r)]
    if not body:
        return ""
    head, *rest = body
    out = ['<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd;">']
    out.append("<tr>" + "".join(f'<th align="left" style="border:1px solid #ddd;background:#f5f5f5;">{_inline_html(c)}</th>' for c in head) + "</tr>")
    for r in rest:
        out.append("<tr>" + "".join(f'<td style="border:1px solid #ddd;">{_inline_html(c)}</td>' for c in r) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """리포트 마크다운을 HTML로 변환 (render_markdown 출력 구조 전용).

    색상은 지정하지 않는다 — 이메일은 클라이언트 기본(검정/흰색), 웹페이지는
    html_document의 스타일(다크모드 포함)이 담당한다.
    """
    html = ['<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.5;">']
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("# "):
            html.append(f"<h2>{_inline_html(line[2:])}</h2>")
            i += 1
        elif line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            html.append(_table_html(block))
        elif line.startswith("- "):
            html.append("<ul>")
            while i < len(lines) and lines[i].startswith("- "):
                html.append(f"<li>{_inline_html(lines[i][2:])}</li>")
                i += 1
            html.append("</ul>")
        else:
            html.append(f"<p>{_inline_html(line)}</p>")
            i += 1
    html.append("</div>")
    return "\n".join(html)


_PAGE_STYLE = """
:root { color-scheme: light dark; }
body { max-width: 860px; margin: 2rem auto; padding: 0 1rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6; }
h2 { border-bottom: 2px solid #8883; padding-bottom: .3rem; }
table { border-collapse: collapse; margin: 1rem 0; font-size: 14px;
  display: block; overflow-x: auto; width: fit-content; max-width: 100%; }
/* markdown_to_html이 이메일용 인라인 스타일(밝은 배경 등)을 넣으므로,
   웹 문서(다크모드 포함)에서는 !important로 페이지 스타일이 이기게 한다 */
th, td { border: 1px solid #8884 !important; padding: 6px 10px; text-align: left;
  white-space: nowrap; }
th { background: #8882 !important; }
ul { padding-left: 1.2rem; }
footer { margin-top: 2rem; color: #8889; font-size: 12px; }
.charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px; margin: 1rem 0; }
.charts svg { width: 100%; height: auto; background: #8881; border-radius: 8px; }
"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _svg_line_chart(
    name: str, sub: str, points: list[dict[str, Any]], width: int = 280, height: int = 100
) -> str:
    """의존성 없이 손으로 그리는 미니 라인 차트 (웹 HTML 전용). 값 2개 미만이면 빈 문자열."""
    pts = [(p["week"], p["vol"]) for p in points if p.get("vol") is not None]
    if len(pts) < 2:
        return ""
    values = [v for _, v in pts]
    lo, hi = min(values), max(values)
    pad_l, pad_r, pad_t, pad_b = 10, 10, 26, 18
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(values)
    xs = [pad_l + pw * i / (n - 1) for i in range(n)]
    if hi - lo < 1e-12:
        ys = [pad_t + ph / 2.0] * n
    else:
        ys = [pad_t + ph * (1 - (v - lo) / (hi - lo)) for v in values]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    first, last = pts[0][0].isoformat(), pts[-1][0].isoformat()
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="{_esc(name)} 주간 추이">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="currentColor">{_esc(name)}</text>'
        f'<text x="{width - pad_r}" y="16" font-size="11" text-anchor="end" fill="currentColor" opacity="0.6">{_esc(sub)}</text>'
        f'<line x1="{pad_l}" y1="{pad_t + ph}" x2="{pad_l + pw}" y2="{pad_t + ph}" stroke="currentColor" opacity="0.15"/>'
        f'<polyline points="{poly}" fill="none" stroke="#5b8def" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3" fill="#5b8def"/>'
        f'<text x="{pad_l}" y="{height - 4}" font-size="10" fill="currentColor" opacity="0.5">{first}</text>'
        f'<text x="{width - pad_r}" y="{height - 4}" font-size="10" text-anchor="end" '
        f'fill="currentColor" opacity="0.5">{last}</text>'
        f"</svg>"
    )


def _charts_html(result: dict[str, Any]) -> str:
    """추이 차트 묶음 — metrics 정렬(시그널 우선, z 내림차순)대로 최대 CHARTS_N개."""
    metrics: list[KeywordMetrics] = result.get("metrics") or []
    history = result.get("history") or {}
    charts: list[str] = []
    for m in metrics:
        svg = _svg_line_chart(
            m.keyword, f"WoW {_fmt_pct(m.naver_velocity)}", history.get(m.keyword, [])
        )
        if svg:
            charts.append(svg)
        if len(charts) >= CHARTS_N:
            break
    if not charts:
        return ""
    return (
        f"<h3>주간 추이 — 네이버 검색 (앵커 보정, 최근 {HISTORY_WEEKS}주)</h3>"
        '<div class="charts">' + "".join(charts) + "</div>"
    )


def html_document(
    markdown_text: str, title: str, generated_at: str = "", result: dict[str, Any] | None = None
) -> str:
    """웹페이지용 완결 HTML 문서 (GitHub Pages 게시용).

    result를 주면 마크다운 본문 뒤에 SVG 추이 차트를 덧붙인다 — 이메일 클라이언트는
    SVG 지원이 불안정하므로(Gmail은 제거함) 차트는 웹 문서에만 넣는다.
    """
    body = markdown_to_html(markdown_text)
    charts = _charts_html(result) if result else ""
    foot = f"<footer>생성 시각: {generated_at}</footer>" if generated_at else ""
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{_PAGE_STYLE}</style>\n"
        f"</head><body>\n{body}\n{charts}\n{foot}\n</body></html>\n"
    )


def send_email(subject: str, markdown: str, recipients: list[str]) -> None:
    if not SMTP_HOST:
        raise SystemExit("SMTP_HOST가 설정되지 않았습니다 (.env 확인)")
    if not recipients:
        raise SystemExit("이메일 수신자(to)가 비어 있습니다 (channels.yaml 확인)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown)  # 플레인 텍스트 대체본
    msg.add_alternative(markdown_to_html(markdown), subtype="html")

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            if SMTP_STARTTLS:
                s.starttls()
                s.ehlo()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def _resolve_webhook(ch: Channel) -> str:
    url = ch.webhook_url or os.environ.get(ch.webhook_env, "")
    if not url:
        raise SystemExit(
            f"slack 채널 '{ch.name}'의 webhook이 없습니다 "
            f"(.env의 {ch.webhook_env} 또는 channels.yaml의 webhook_url 확인)"
        )
    return url


def deliver(
    text: str, subject: str, channels: list[Channel], only: set[str] | None = None
) -> int:
    """enabled 채널(선택적으로 only 이름 필터)로 리포트를 전송. 채널별 실패는 격리."""
    targets = [c for c in channels if c.enabled and (only is None or c.name in only)]
    if only:
        missing = only - {c.name for c in channels}
        for m in missing:
            print(f"[경고] '{m}' 채널이 channels.yaml에 없습니다")
    if not targets:
        print("전송 대상이 없습니다 — channels.yaml에서 enabled: true로 설정했는지 확인하세요")
        return 1

    failed = []
    for ch in targets:
        try:
            if ch.type == "slack":
                send_slack(text, _resolve_webhook(ch))
            elif ch.type == "email":
                send_email(ch.subject or subject, text, list(ch.to))
            print(f"[{ch.type}:{ch.name}] 전송 완료")
        except Exception as e:  # noqa: BLE001 — 한 채널 실패가 나머지 전송을 막지 않게
            print(f"[{ch.type}:{ch.name}] 전송 실패: {e}")
            failed.append(ch.name)
    return 1 if failed else 0
