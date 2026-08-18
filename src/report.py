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


# 유튜브 video_id는 정확히 11자 [A-Za-z0-9_-]. 임베드/링크 URL에 넣기 전 반드시 검증한다.
# (외부에서 온 값이라 신뢰하지 않는다 — 검증 실패 시 임베드/링크를 생략한다.)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _valid_youtube_id(vid: str | None) -> bool:
    return bool(vid and _YOUTUBE_ID_RE.match(vid))


CAPTION_MAX = 90
CAPTION_INPUT_MAX = 2000  # 스캔 전 상한 (인스타 캡션 최대 2200자)
# 인스타 게시물 URL만 허용 — 액터 출력을 그대로 믿지 않는다(스킴/도메인 인젝션 차단)
_IG_URL_RE = re.compile(r"^https://(?:www\.)?instagram\.com/[A-Za-z0-9_\-/.]+/?$")


def _safe_ig_url(url: str | None) -> bool:
    return bool(url and _IG_URL_RE.match(url))


def _visible_reels(reels: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """표시 가능한 릴스만 남기고 순위를 다시 매긴다.

    URL 화이트리스트에서 걸러진 항목이 순위를 차지한 채 빠지면 #1이 없고 #3부터
    시작하는 목록이 나오므로, 필터 후에 1..N으로 재부여한다.
    """
    out = []
    for r in reels or []:
        if _safe_ig_url(r.get("url")):
            out.append({**r, "rank": len(out) + 1})
    return out


def _caption_summary(caption: str | None, limit: int = CAPTION_MAX) -> str:
    """캡션을 한 줄 설명으로 압축 — 줄바꿈/해시태그 뭉치를 정리하고 길이를 자른다.

    끝에 몰린 해시태그는 토큰을 뒤에서 떼어내 제거한다. 정규식(`(?:#\\S+\\s*)+$`)을 쓰면
    \\S가 '#'까지 삼켜 분할 경우의 수가 지수로 늘어나 역추적 폭발이 일어난다 — 실측으로
    '#a'가 24번 붙은 50자 입력이 3.3초, 토큰당 약 4배씩 증가했다. 캡션은 인스타 사용자가
    자유롭게 쓰는 값(최대 2200자)이고 한국어 게시물에서 해시태그를 붙여 쓰는 표기가 흔해
    악의가 없어도 리포트 생성이 멈출 수 있다. 스캔 전에 길이도 제한한다.
    """
    if not caption:
        return "(설명 없음)"
    text = " ".join(caption.split())[:CAPTION_INPUT_MAX]
    tokens = text.split(" ")
    while tokens and tokens[-1].startswith("#"):
        tokens.pop()
    text = " ".join(tokens).strip() or text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_handle(owner: Any) -> str:
    """인스타 사용자명을 표시 가능한 형태로 제한 — 공백/개행이 섞이면 마크다운 줄 구조가 깨진다."""
    return re.sub(r"[^A-Za-z0-9._]", "", str(owner or ""))[:30]


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


def render_markdown(result: dict[str, Any], omit_rich: bool = False) -> str:
    """리포트 마크다운.

    omit_rich=True면 HTML 문서가 자체 섹션(릴스 카드·영상 갤러리)으로 다시 그리는 항목을
    생략한다 — 그러지 않으면 웹 리포트에 같은 목록이 두 번 나온다.
    """
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

    tops = [] if omit_rich else [
        t for t in (result.get("youtube_top_videos") or []) if _valid_youtube_id(t.get("video_id"))
    ]
    if tops:
        # 텍스트 리포트(터미널/Slack/이메일)에는 iframe 대신 안전한 링크 목록만.
        # URL은 검증된 id로 직접 조립한다. 제목은 평문(HTML 변환 시 이스케이프됨).
        lines.append("")
        lines.append(f"**🔥 최근 7일 조회 급상승 영상 톱 {len(tops)} (유튜브)**")
        for t in tops:
            url = f"https://www.youtube.com/watch?v={t['video_id']}"
            title = t.get("title") or "YouTube 동영상"
            channel = t.get("channel") or ""
            dv = t.get("dviews")
            extra = f", 최근 7일 +{dv:,}뷰" if dv is not None else ""
            lines.append(f"- #{t['rank']} {title} ({channel}{extra}) — {url}")

    reels = [] if omit_rich else _visible_reels(result.get("instagram_reels"))
    if reels:
        lines.append("")
        lines.append(f"**📸 인스타그램 인기 릴스 톱 {len(reels)}**")
        for r in reels:
            parts = []
            if _clean_handle(r.get("owner")):
                parts.append(f"@{_clean_handle(r['owner'])}")
            if r.get("views") is not None:
                parts.append(f"조회 {r['views']:,}")
            if r.get("likes") is not None:
                parts.append(f"좋아요 {r['likes']:,}")
            meta = f" ({', '.join(parts)})" if parts else ""
            lines.append(f"- #{r['rank']} {_caption_summary(r.get('caption'))}{meta} — {r['url']}")

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
.videos { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px; margin: 1rem 0; align-items: start; }
.video-card { margin: 0; }
.video-card iframe { width: 100%; aspect-ratio: 16 / 9; height: auto; border: 0;
  border-radius: 8px; display: block; background: #8881; }
.video-card figcaption { margin-top: .5rem; }
.video-card .vtitle { margin: 0 0 .2rem; font-size: 14px; font-weight: 600; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.video-card .rank { display: inline-block; min-width: 1.5em; padding: 0 .35em; margin-right: .3em;
  border-radius: 4px; background: #5b8def; color: #fff; text-align: center; font-size: 12px; }
.video-card .vmeta { margin: 0; font-size: 12px; opacity: .85; }
.video-card .vsub { margin: .15rem 0 0; font-size: 12px; opacity: .6; }
.reels { list-style: none; padding: 0; margin: 1rem 0; }
.reels .reel { display: flex; align-items: baseline; gap: .5rem; padding: .5rem 0;
  border-bottom: 1px solid #8883; flex-wrap: wrap; }
.reels .reel a { font-weight: 500; text-decoration: none; }
.reels .reel a:hover { text-decoration: underline; }
.reels .rmeta { font-size: 12px; opacity: .65; white-space: nowrap; }
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


def _youtube_card_html(t: dict[str, Any]) -> str:
    """급상승 영상 1개의 카드 = 공식 임베드 플레이어 + 요약(순위·제목·채널·증가분·총뷰·키워드).

    보안: video_id를 11자 [A-Za-z0-9_-]로 검증한 뒤에만 사용하고(실패 시 빈 문자열 → 갤러리에서
    제외), src/watch URL은 검증된 id로 직접 조립한다 — 외부 URL을 통과시키지 않으므로 스킴/속성
    인젝션 여지가 없다. 제목·채널·키워드 등 신뢰 불가 문자열은 요소 본문에만 넣고 _esc로
    이스케이프한다(속성에는 두지 않는다). rank는 정수로 캐스팅해 사용한다.
    """
    if not _valid_youtube_id(t.get("video_id")):
        return ""
    vid = t["video_id"]
    src = f"https://www.youtube-nocookie.com/embed/{vid}"
    watch = f"https://www.youtube.com/watch?v={vid}"
    title = _esc(t.get("title") or "YouTube 동영상")
    rank = int(t.get("rank") or 0)

    meta = []
    if t.get("dviews") is not None:
        meta.append(f"🔥 최근 7일 +{t['dviews']:,}뷰")
    if t.get("views") is not None:
        meta.append(f"총 {t['views']:,}뷰")
    sub = []
    if t.get("channel"):
        sub.append(_esc(t["channel"]))
    kws = ", ".join(_esc(k) for k in (t.get("keywords") or []) if k)
    if kws:
        sub.append(f"키워드: {kws}")
    return (
        '<figure class="video-card">'
        f'<iframe src="{src}" title="YouTube 동영상 플레이어" loading="lazy" '
        'referrerpolicy="strict-origin-when-cross-origin" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; '
        'picture-in-picture; web-share" allowfullscreen></iframe>'
        "<figcaption>"
        f'<p class="vtitle"><span class="rank">#{rank}</span>'
        f'<a href="{watch}" target="_blank" rel="noopener noreferrer nofollow">{title}</a></p>'
        f'<p class="vmeta">{" · ".join(meta)}</p>'
        f'<p class="vsub">{" · ".join(sub)}</p>'
        "</figcaption>"
        "</figure>"
    )


def _youtube_gallery_html(tops: list[dict[str, Any]] | None) -> str:
    """급상승 영상 톱 N 갤러리 (HTML 문서 전용).

    법적: 공식 임베드 플레이어만 사용(다운로드·재호스팅 없음), 제목은 watch 페이지로 링크해
    출처를 표기한다. 이메일/Slack에는 넣지 않는다 — 클라이언트가 iframe을 제거·미지원하므로
    거기서는 render_markdown의 링크 목록으로 대체한다. 각 카드는 화면에 들어올 때만 로드되도록
    loading=lazy를 쓴다(임베드 10개의 초기 로드·추적 최소화).
    """
    cards = [c for c in (_youtube_card_html(t) for t in (tops or [])) if c]
    if not cards:
        return ""
    return (
        f"<h3>🔥 최근 7일 조회 급상승 영상 톱 {len(cards)}</h3>"
        '<div class="videos">' + "".join(cards) + "</div>"
    )


def _reels_html(reels: list[dict[str, Any]] | None) -> str:
    """인기 릴스 목록 (HTML 문서 전용).

    인스타 임베드(oEmbed)는 App Review가 필요하고 썸네일 핫링크는 만료 URL이라,
    링크 + 설명 카드로만 구성한다 — 약관상 안전하고 깨지지 않는다.
    URL은 instagram.com 게시물 형태만 통과시키고(_safe_ig_url), 캡션은 요소 본문에만 넣어
    이스케이프한다(속성에 신뢰 불가 문자열을 두지 않는다).
    """
    items = _visible_reels(reels)
    if not items:
        return ""
    cards = []
    for r in items:
        bits = []
        if _clean_handle(r.get("owner")):
            bits.append(f"@{_esc(_clean_handle(r['owner']))}")
        if r.get("views") is not None:
            bits.append(f"▶ {r['views']:,}")
        if r.get("likes") is not None:
            bits.append(f"♥ {r['likes']:,}")
        if r.get("comments") is not None:
            bits.append(f"💬 {r['comments']:,}")
        cards.append(
            '<li class="reel">'
            f'<span class="rank">#{int(r["rank"])}</span>'
            f'<a href="{r["url"]}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{_esc(_caption_summary(r.get("caption")))}</a>'
            f'<span class="rmeta">{" · ".join(bits)}</span>'
            "</li>"
        )
    return (
        f"<h3>📸 인스타그램 인기 릴스 톱 {len(cards)}</h3>"
        '<ol class="reels">' + "".join(cards) + "</ol>"
    )


def html_document(
    markdown_text: str, title: str, generated_at: str = "", result: dict[str, Any] | None = None
) -> str:
    """웹페이지용 완결 HTML 문서 (GitHub Pages 게시용).

    result를 주면 마크다운 본문 뒤에 급상승 영상 갤러리 + SVG 추이 차트를 덧붙인다 —
    iframe·SVG는 이메일 클라이언트 지원이 불안정하므로(Gmail은 제거함) 웹 문서에만 넣는다.
    """
    body = markdown_to_html(markdown_text)
    gallery = _youtube_gallery_html(result.get("youtube_top_videos")) if result else ""
    reels = _reels_html(result.get("instagram_reels")) if result else ""
    charts = _charts_html(result) if result else ""
    foot = f"<footer>생성 시각: {generated_at}</footer>" if generated_at else ""
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{_PAGE_STYLE}</style>\n"
        f"</head><body>\n{body}\n{reels}\n{gallery}\n{charts}\n{foot}\n</body></html>\n"
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
