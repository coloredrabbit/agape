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
        lines.append(f"**급등 시그널 톱 {len(flagged)}**")
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
        lines.append("**급등 시그널**")
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


# 시그널 라벨 → 알약(pill) 클래스. metrics.KeywordMetrics.signal이 내는 값과 맞춰야 한다.
_SIGNAL_PILL = {"강한 후보": "pill-strong", "관찰": "pill-watch", "계절성 의심": "pill-season"}


def _cell_html(c: str) -> str:
    """웹 표의 셀 1개 — 시그널은 알약으로, 스파크라인은 등폭·강조색으로 감싼다.

    이메일 경로(_table_html의 기본 모드)는 이 변환을 쓰지 않는다 — 클라이언트가 클래스를
    무시하므로 평문이 더 안전하다.
    """
    if c in _SIGNAL_PILL:
        return f'<span class="pill {_SIGNAL_PILL[c]}">{_inline_html(c)}</span>'
    if c and all(ch in SPARK_CHARS for ch in c):
        return f'<span class="spark">{_inline_html(c)}</span>'
    return _inline_html(c)


def _table_html(block: list[str], rich: bool = False) -> str:
    """마크다운 표 → HTML.

    rich=False(이메일): 인라인 스타일로 테두리를 그린다 — 메일 클라이언트는 <style>을
    자주 제거하므로 인라인이 유일하게 믿을 수 있는 수단이다.
    rich=True(웹): 스타일을 전혀 넣지 않고 클래스만 남겨 페이지 CSS가 그린다. 덕분에
    페이지 쪽에서 !important로 인라인 스타일과 싸울 필요가 없다.
    """
    rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in block]
    body = [r for r in rows if not all(re.fullmatch(r":?-{3,}:?", c or "") for c in r)]
    if not body:
        return ""
    head, *rest = body
    if rich:
        out = ['<div class="table-wrap"><table>']
        out.append("<thead><tr>" + "".join(f"<th>{_inline_html(c)}</th>" for c in head) + "</tr></thead>")
        out.append("<tbody>")
        for r in rest:
            out.append("<tr>" + "".join(f"<td>{_cell_html(c)}</td>" for c in r) + "</tr>")
        out.append("</tbody></table></div>")
        return "\n".join(out)
    out = ['<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd;">']
    out.append("<tr>" + "".join(f'<th align="left" style="border:1px solid #ddd;background:#f5f5f5;">{_inline_html(c)}</th>' for c in head) + "</tr>")
    for r in rest:
        out.append("<tr>" + "".join(f'<td style="border:1px solid #ddd;">{_inline_html(c)}</td>' for c in r) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """리포트 마크다운을 HTML로 변환 — **이메일 전용** (render_markdown 출력 구조 전용).

    색상은 지정하지 않는다 — 클라이언트 기본(검정/흰색)을 따르게 한다.
    웹페이지는 이 함수를 쓰지 않고 _body_cards_html이 카드 레이아웃으로 다시 그린다.
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


# 한 줄 전체가 굵게인 줄 = 섹션 제목. "**시그널** 강한 후보 0 · ..." 처럼 뒤에 본문이
# 이어지는 줄은 제목이 아니므로 걸리지 않는다($ 앵커).
_SECTION_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*$")
_NAV_LABEL_MAX = 20


def _nav_label(title: str) -> str:
    """사이드바용 짧은 라벨 — 첫 구분자(— / () 앞까지 자르고 길이를 제한한다."""
    label = re.split(r"\s+[—–-]\s+|\s*\(", title, maxsplit=1)[0].strip() or title
    return label if len(label) <= _NAV_LABEL_MAX else label[: _NAV_LABEL_MAX - 1] + "…"


_ITALIC_LINE_RE = re.compile(r"^_(.+)_$")


def _md_sections(md: str) -> tuple[str, list[str], list[dict[str, Any]], list[str]]:
    """리포트 마크다운을 (문서 제목, 리드 문단, 섹션 목록)으로 쪼갠다.

    섹션 경계는 `## 제목`(focus 대시보드)과 한 줄 전체 굵게(`**제목**`, 주간 리포트) 둘 다.
    최상단 `# 제목`은 상단바로 올리고, `**시그널** ...` 요약 줄은 KPI 카드가 대체하므로 버린다.
    제목 없이 시작하는 앞부분(focus의 기간 안내 등)은 리드 문단으로 따로 돌려준다.
    한 줄 전체가 기울임(`_..._`)인 줄은 지표 설명 각주다 — 섹션이 시작되기 전이면 리드로,
    이후에 나오면(주간 리포트 맨 끝의 용어 설명) 마지막 섹션에 섞이지 않게 하단 각주로 뺀다.
    """
    doc_title, lead, notes = "", [], []
    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            doc_title = line[2:].strip()
            continue
        heading = None
        if line.startswith("### "):
            heading = line[4:].strip()
        elif line.startswith("## "):
            heading = line[3:].strip()
        else:
            m = _SECTION_BOLD_RE.match(line)
            if m:
                heading = m.group(1).strip()
        if heading:
            cur = {"title": heading, "id": f"s{len(sections) + 1}", "lines": []}
            sections.append(cur)
            continue
        if line.startswith("**시그널**"):
            continue  # KPI 카드가 같은 수치를 보여준다
        if _ITALIC_LINE_RE.match(line):
            (notes if sections else lead).append(line)
            continue
        (cur["lines"] if cur else lead).append(line)
    return doc_title, lead, [s for s in sections if s["lines"]], notes


def _lines_html(lines: list[str]) -> str:
    """섹션 본문(표·목록·문단)을 웹용 HTML로."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table_html(block, rich=True))
        elif line.startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].startswith("- "):
                out.append(f"<li>{_inline_html(lines[i][2:])}</li>")
                i += 1
            out.append("</ul>")
        else:
            cls = ' class="note"' if line.startswith("→") else ""
            out.append(f"<p{cls}>{_inline_html(line)}</p>")
            i += 1
    return "\n".join(out)


def _body_cards_html(md: str) -> tuple[str, str, str, list[tuple[str, str]], str]:
    """마크다운 → (문서 제목, 리드 HTML, 카드 HTML, 내비 항목, 각주 HTML). 웹 문서 전용."""
    doc_title, lead, sections, notes = _md_sections(md)
    lead_html = f'<p class="lead">{_inline_html(" ".join(lead))}</p>' if lead else ""
    cards = [
        f'<section class="card" id="{s["id"]}">'
        f'<h2 class="card-title">{_inline_html(s["title"])}</h2>'
        f'{_lines_html(s["lines"])}</section>'
        for s in sections
    ]
    nav = [(s["id"], _nav_label(s["title"])) for s in sections]
    notes_html = "".join(f"<p>{_inline_html(n)}</p>" for n in notes)
    return doc_title, lead_html, "\n".join(cards), nav, notes_html


_PAGE_STYLE = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  color-scheme: light;
  /* 대비: --muted는 카드(#fff) 4.96:1 · 페이지(#f4f6fb) 4.59:1 · 표헤더(#fbfcfe) 4.83:1
     로 WCAG AA(4.5:1)를 모든 배경에서 넘긴다. 여기서 더 밝히면 11~13px 소형 텍스트가
     읽히지 않는다(이전 #98a2b8은 2.56:1이었다). --ink-2는 5.80:1 — 표 열 이름·KPI 레이블처럼
     "숫자를 해석하는 데 필요한" 텍스트에 쓴다. */
  --bg: #f4f6fb; --card: #fff; --ink: #1f2a44; --ink-2: #5b6580; --muted: #657089;
  --line: #eceff6; --blue: #3b5bfe; --blue-ink: #2843d6; --blue-50: #eff2ff;
  --amber-50: #fff5e6; --amber-ink: #b45309;
  --radius: 14px; --shadow: 0 1px 2px rgba(31,42,68,.04), 0 6px 22px rgba(31,42,68,.06);
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo",
    "Malgun Gothic", Roboto, sans-serif;
}
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans);
  font-size: 14px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
a { color: var(--blue); }

/* ── 레이아웃 ─────────────────────────────────────────── */
.app { display: grid; grid-template-columns: 252px minmax(0, 1fr); min-height: 100vh; }
.side { background: #fff; border-right: 1px solid var(--line); padding: 26px 0 24px;
  position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto; }
.main { padding: 26px 30px 64px; min-width: 0; }
.wrap { max-width: 1160px; margin: 0 auto; }

/* ── 사이드바 ─────────────────────────────────────────── */
.brand { display: flex; align-items: center; gap: 10px; padding: 0 22px 22px; }
.brand .mark { width: 36px; height: 36px; border-radius: 11px; background: var(--blue);
  display: grid; place-items: center; color: #fff; flex: 0 0 auto;
  box-shadow: 0 4px 12px rgba(59,91,254,.32); }
.brand b { font-size: 17px; letter-spacing: -.01em; display: block; }
.brand span { font-size: 11.5px; color: var(--muted); }
.nav { list-style: none; margin: 0; padding: 0 12px; }
.nav a { display: flex; align-items: center; gap: 10px; padding: 10px 12px; margin-bottom: 2px;
  border-radius: 10px; color: var(--ink-2); text-decoration: none; font-size: 13.5px;
  font-weight: 500; transition: background .15s, color .15s; }
.nav a:hover { background: #f6f7fb; color: var(--ink); }
.nav a.on { background: var(--blue-50); color: var(--blue-ink); font-weight: 600; }
.nav a.on .dot { background: var(--blue); }
.nav .dot { width: 6px; height: 6px; border-radius: 50%; background: #cfd5e4; flex: 0 0 auto; }
.side-foot { padding: 18px 22px 0; margin-top: 14px; border-top: 1px solid var(--line);
  font-size: 11.5px; color: var(--muted); }

/* ── 상단바 ───────────────────────────────────────────── */
.top { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px;
  flex-wrap: wrap; margin-bottom: 20px; }
.top h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -.02em; }
.top .sub { margin: 4px 0 0; font-size: 13px; color: var(--muted); }
.chip { background: #fff; border: 1px solid var(--line); border-radius: 999px;
  padding: 7px 14px; font-size: 12px; color: var(--ink-2); box-shadow: var(--shadow);
  white-space: nowrap; }
.lead { color: var(--ink-2); font-size: 13px; margin: 0 0 18px; }

/* ── KPI 카드 ─────────────────────────────────────────── */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
  gap: 16px; margin-bottom: 22px; }
.kpi { background: var(--card); border-radius: var(--radius); box-shadow: var(--shadow);
  padding: 20px 18px; text-align: center; }
/* display:block인 인라인 SVG는 부모의 text-align:center로 가운데 오지 않는다 —
   블록 요소라 margin auto가 필요하다(이게 없어 아이콘이 좌측에 붙어 있었다). */
.kpi .ico { color: var(--blue); display: block; margin: 0 auto 8px; }
.kpi .n { font-size: 30px; font-weight: 700; letter-spacing: -.03em; line-height: 1.1; }
.kpi .l { font-size: 13px; color: var(--ink-2); margin-top: 3px; font-weight: 500; }
.kpi .h { font-size: 11px; color: var(--muted); margin-top: 6px; }
.kpi.hot { background: var(--blue); color: #fff;
  box-shadow: 0 6px 22px rgba(59,91,254,.34); }
.kpi.hot .ico { color: rgba(255,255,255,.9); }
.kpi.hot .l { color: #fff; }
/* 채도 높은 파란 배경 위에서는 흰색을 흐리게 하면 AA를 못 넘는다(.68 = 3.22:1).
   .92 = 4.57:1로 올리고 위계는 색이 아니라 글자 크기·굵기로 준다. */
.kpi.hot .h { color: rgba(255,255,255,.92); }

/* ── 카드 ─────────────────────────────────────────────── */
.card { background: var(--card); border-radius: var(--radius); box-shadow: var(--shadow);
  padding: 22px 24px; margin-bottom: 20px; scroll-margin-top: 20px; }
.card-title { margin: 0 0 14px; font-size: 15.5px; font-weight: 700; letter-spacing: -.01em; }
.card > p { margin: 0 0 10px; }
.card > p:last-child { margin-bottom: 0; }
.card p.note { background: var(--blue-50); border-left: 3px solid var(--blue);
  border-radius: 0 8px 8px 0; padding: 10px 14px; color: #35406a; }
.card ul { margin: 0; padding-left: 0; list-style: none; }
.card ul li { padding: 9px 2px; border-bottom: 1px solid var(--line); }
.card ul li:last-child { border-bottom: 0; padding-bottom: 0; }

/* ── 표 ───────────────────────────────────────────────── */
.table-wrap { overflow-x: auto; margin: 0 -6px; }
.card table { border-collapse: separate; border-spacing: 0; width: 100%;
  font-size: 13px; min-width: max-content; }
.card th, .card td { padding: 11px 12px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--line); }
.card thead th { color: var(--ink-2); font-weight: 600; font-size: 11.5px;
  text-transform: uppercase; letter-spacing: .04em; background: #fbfcfe; }
.card thead th:first-child { border-radius: 8px 0 0 8px; }
.card thead th:last-child { border-radius: 0 8px 8px 0; }
.card tbody tr:last-child td { border-bottom: 0; }
.card tbody tr:hover td { background: #fafbff; }
.card td:first-child { font-weight: 500; }
.pill { display: inline-block; padding: 3px 10px; border-radius: 999px;
  font-size: 11.5px; font-weight: 600; white-space: nowrap; }
.pill-strong { background: #e7ecff; color: var(--blue-ink); }
.pill-watch { background: var(--amber-50); color: var(--amber-ink); }
.pill-season { background: #f1f3f8; color: #6b7691; }
.spark { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--blue);
  letter-spacing: 1px; font-size: 14px; }

/* ── 차트 ─────────────────────────────────────────────── */
.charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(252px, 1fr));
  gap: 14px; }
.charts .ch { border: 1px solid var(--line); border-radius: 12px; padding: 6px; }
.charts svg { width: 100%; height: auto; display: block; }

/* ── 유튜브 ───────────────────────────────────────────── */
.videos { display: grid; grid-template-columns: repeat(auto-fill, minmax(288px, 1fr));
  gap: 18px; align-items: start; }
.video-card { margin: 0; }
.video-card iframe { width: 100%; aspect-ratio: 16 / 9; height: auto; border: 0;
  border-radius: 10px; display: block; background: #eef1f7; }
.video-card figcaption { margin-top: 10px; }
.video-card .vtitle { margin: 0 0 4px; font-size: 13.5px; font-weight: 600; line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.video-card .vtitle a { color: var(--ink); text-decoration: none; }
.video-card .vtitle a:hover { color: var(--blue); }
.video-card .vmeta { margin: 0; font-size: 12px; color: var(--ink-2); }
.video-card .vsub { margin: 3px 0 0; font-size: 11.5px; color: var(--muted); }
.rank { display: inline-block; min-width: 21px; padding: 1px 6px; margin-right: 6px;
  border-radius: 6px; background: var(--blue-50); color: var(--blue-ink);
  text-align: center; font-size: 11.5px; font-weight: 700; }

/* ── 릴스 ─────────────────────────────────────────────── */
.reels { list-style: none; padding: 0; margin: 0; }
.reels .reel { display: flex; align-items: center; gap: 10px; padding: 11px 8px;
  border-radius: 10px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.reels .reel:last-child { border-bottom: 0; }
.reels .reel:hover { background: #fafbff; }
/* 캡션은 한 줄로 자른다 — 2줄 이상 흐르면 행 높이가 들쭉날쭉해져 목록 리듬이 깨진다.
   title 속성으로 전문을 노출하지는 않는다: _esc는 따옴표를 이스케이프하지 않으므로
   신뢰 불가 문자열을 속성에 두면 속성 탈출 위험이 생긴다(본문에만 넣는 기존 원칙 유지). */
.reels .reel > a { font-weight: 500; text-decoration: none; color: var(--ink); flex: 1 1 260px;
  min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.reels .reel > a:hover { color: var(--blue); }
.reels .rmeta { font-size: 11.5px; color: var(--muted); white-space: nowrap;
  font-variant-numeric: tabular-nums; }

footer { color: var(--ink-2); font-size: 12px; margin-top: 24px; }

/* ── 좁은 화면: 사이드바를 상단 가로 내비로 ───────────── */
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .side { position: static; max-height: none; border-right: 0;
    border-bottom: 1px solid var(--line); padding: 18px 0 12px; }
  .brand { padding: 0 18px 14px; }
  .nav { display: flex; gap: 6px; overflow-x: auto; padding: 0 14px 4px; }
  .nav a { white-space: nowrap; }
  .nav a .dot { display: none; }
  .side-foot { display: none; }
  .main { padding: 20px 16px 48px; }
  .card { padding: 18px 16px; }
}
"""

# 스크롤에 따라 사이드바 항목을 강조. JS가 없어도 페이지는 그대로 동작한다(단순 앵커 목록).
_PAGE_SCRIPT = """
(function () {
  var links = [].slice.call(document.querySelectorAll('.nav a'));
  var byId = {};
  links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
  var targets = Object.keys(byId).map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  if (!targets.length || !window.IntersectionObserver) return;
  var seen = {};
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { seen[e.target.id] = e.isIntersecting; });
    for (var i = 0; i < targets.length; i++) {
      if (seen[targets[i].id]) {
        links.forEach(function (a) { a.classList.remove('on'); });
        byId[targets[i].id].classList.add('on');
        break;
      }
    }
  }, { rootMargin: '-72px 0px -65% 0px' });
  targets.forEach(function (t) { io.observe(t); });
})();
"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(s: str) -> str:
    """속성값용 이스케이프 — _esc에 더해 따옴표까지 막는다.

    _esc는 &, <, >만 치환하므로 속성 컨텍스트에서는 따옴표 하나로 탈출이 가능하다. 그래서
    이 코드베이스는 "신뢰 불가 문자열은 속성이 아니라 요소 본문에만" 원칙을 쓰는데,
    차트 SVG의 aria-label은 접근성상 속성에 넣어야 해서 그 한 자리를 위해 둔다. 키워드는
    keywords.yaml에서 검증 없이 들어오고 산출물은 Pages로 공개 게시되므로, 따옴표가 섞이면
    인라인 <svg>의 on* 속성이 실제로 실행된다(실측).

    이메일 경로는 _esc/_inline_html만 쓰므로 이 함수 추가는 이메일 출력에 영향이 없다.
    """
    return _esc(s).replace('"', "&quot;").replace("'", "&#39;")


_ICONS = {
    "spark": "M13 2 4.5 13H11l-1 9 8.5-11H12l1-9Z",
    "eye": "M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
    "repeat": "M17 2l4 4-4 4 M21 6H8a5 5 0 0 0-5 5 M7 22l-4-4 4-4 M3 18h13a5 5 0 0 0 5-5",
    "trend": "M3 17l6-6 4 4 8-8 M15 7h6v6",
    "tag": "M3 5h8l10 7-10 7H3z M7.5 12h.01",
    "logo": "M12 2c3 4.5 5.5 7 5.5 11a5.5 5.5 0 1 1-11 0C6.5 9 9 6.5 12 2Z",
}


def _icon(name: str, size: int = 21) -> str:
    """의존성 없는 인라인 스트로크 아이콘 (currentColor 상속)."""
    return (
        f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true"><path d="{_ICONS[name]}"/></svg>'
    )


def _kpi_cards(result: dict[str, Any]) -> str:
    """상단 KPI 카드 — render_markdown의 시그널 요약 한 줄을 대체한다(같은 수치)."""
    metrics: list[KeywordMetrics] = result.get("metrics") or []
    counts = {"강한 후보": 0, "관찰": 0, "계절성 의심": 0}
    for m in metrics:
        if m.signal:
            counts[m.signal] += 1
    vel = [m.naver_velocity for m in metrics if m.naver_velocity is not None]
    ups = sum(1 for v in vel if v > 0)
    downs = sum(1 for v in vel if v < 0)
    naver_n = sum(1 for m in metrics if m.naver_vol is not None)
    yt_n = sum(1 for m in metrics if m.yt_views is not None)
    pin_n = sum(1 for m in metrics if m.pin_mom is not None)

    cards = [
        ("spark", counts["강한 후보"], "강한 후보", "네이버 급등 + 동반 상승", True),
        ("eye", counts["관찰"], "관찰", "단일 소스 급등", False),
        ("repeat", counts["계절성 의심"], "계절성 의심", "작년에도 이맘때 높음", False),
        ("trend", ups, "WoW 상승", f"하락 {downs}개", False),
        ("tag", len(metrics), "추적 키워드", f"네이버 {naver_n} · 유튜브 {yt_n} · 핀 {pin_n}", False),
    ]
    out = []
    for ico, n, label, hint, hot in cards:
        out.append(
            f'<div class="kpi{" hot" if hot else ""}">{_icon(ico)}'
            f'<div class="n">{n}</div><div class="l">{_esc(label)}</div>'
            f'<div class="h">{_esc(hint)}</div></div>'
        )
    return '<div class="kpis">' + "".join(out) + "</div>"


def _svg_line_chart(
    name: str,
    sub: str,
    points: list[dict[str, Any]],
    width: int = 280,
    height: int = 108,
    uid: str = "c0",
) -> str:
    """의존성 없이 손으로 그리는 미니 라인 차트 (웹 HTML 전용). 값 2개 미만이면 빈 문자열.

    uid는 <linearGradient> id 충돌을 막는다 — 한 페이지에 차트가 여러 개 들어가므로
    같은 id를 쓰면 뒤 차트가 앞 차트의 그라디언트를 덮어쓴다.
    """
    pts = [(p["week"], p["vol"]) for p in points if p.get("vol") is not None]
    if len(pts) < 2:
        return ""
    values = [v for _, v in pts]
    lo, hi = min(values), max(values)
    pad_l, pad_r, pad_t, pad_b = 8, 8, 30, 18
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(values)
    xs = [pad_l + pw * i / (n - 1) for i in range(n)]
    if hi - lo < 1e-12:
        ys = [pad_t + ph / 2.0] * n
    else:
        ys = [pad_t + ph * (1 - (v - lo) / (hi - lo)) for v in values]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{poly} {xs[-1]:.1f},{pad_t + ph:.1f} {xs[0]:.1f},{pad_t + ph:.1f}"
    grid = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + ph * f:.1f}" x2="{pad_l + pw}" '
        f'y2="{pad_t + ph * f:.1f}" stroke="#eceff6" stroke-width="1"/>'
        for f in (0.0, 0.5, 1.0)
    )
    first, last = pts[0][0].isoformat(), pts[-1][0].isoformat()
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="{_attr(name)} 주간 추이">'
        f'<defs><linearGradient id="g{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#3b5bfe" stop-opacity="0.20"/>'
        f'<stop offset="100%" stop-color="#3b5bfe" stop-opacity="0"/></linearGradient></defs>'
        f'<text x="{pad_l}" y="17" font-size="12.5" font-weight="700" fill="#1f2a44">{_esc(name)}</text>'
        f'<text x="{width - pad_r}" y="17" font-size="11" text-anchor="end" fill="#657089">{_esc(sub)}</text>'
        f"{grid}"
        f'<polygon points="{area}" fill="url(#g{uid})"/>'
        f'<polyline points="{poly}" fill="none" stroke="#3b5bfe" stroke-width="2.2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.6" fill="#fff" stroke="#3b5bfe" stroke-width="2.2"/>'
        f'<text x="{pad_l}" y="{height - 5}" font-size="10" fill="#657089">{first}</text>'
        f'<text x="{width - pad_r}" y="{height - 5}" font-size="10" text-anchor="end" '
        f'fill="#657089">{last}</text>'
        f"</svg>"
    )


def _card(title: str, body: str, sid: str) -> str:
    return (
        f'<section class="card" id="{sid}">'
        f'<h2 class="card-title">{_esc(title)}</h2>{body}</section>'
    )


def _charts_html(result: dict[str, Any], sid: str = "charts") -> str:
    """추이 차트 묶음 — metrics 정렬(시그널 우선, z 내림차순)대로 최대 CHARTS_N개."""
    metrics: list[KeywordMetrics] = result.get("metrics") or []
    history = result.get("history") or {}
    charts: list[str] = []
    for m in metrics:
        svg = _svg_line_chart(
            m.keyword,
            f"WoW {_fmt_pct(m.naver_velocity)}",
            history.get(m.keyword, []),
            uid=f"{sid}{len(charts)}",
        )
        if svg:
            charts.append(f'<div class="ch">{svg}</div>')
        if len(charts) >= CHARTS_N:
            break
    if not charts:
        return ""
    return _card(
        f"주간 추이 — 네이버 검색 (앵커 보정, 최근 {HISTORY_WEEKS}주)",
        '<div class="charts">' + "".join(charts) + "</div>",
        sid,
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
        f'<p class="vtitle"><span class="rank">{rank}</span>'
        f'<a href="{watch}" target="_blank" rel="noopener noreferrer nofollow">{title}</a></p>'
        f'<p class="vmeta">{" · ".join(meta)}</p>'
        f'<p class="vsub">{" · ".join(sub)}</p>'
        "</figcaption>"
        "</figure>"
    )


def _youtube_gallery_html(tops: list[dict[str, Any]] | None, sid: str = "videos") -> str:
    """급상승 영상 톱 N 갤러리 (HTML 문서 전용).

    법적: 공식 임베드 플레이어만 사용(다운로드·재호스팅 없음), 제목은 watch 페이지로 링크해
    출처를 표기한다. 이메일/Slack에는 넣지 않는다 — 클라이언트가 iframe을 제거·미지원하므로
    거기서는 render_markdown의 링크 목록으로 대체한다. 각 카드는 화면에 들어올 때만 로드되도록
    loading=lazy를 쓴다(임베드 10개의 초기 로드·추적 최소화).
    """
    cards = [c for c in (_youtube_card_html(t) for t in (tops or [])) if c]
    if not cards:
        return ""
    return _card(
        f"🔥 최근 7일 조회 급상승 영상 톱 {len(cards)}",
        '<div class="videos">' + "".join(cards) + "</div>",
        sid,
    )


def _reels_html(reels: list[dict[str, Any]] | None, sid: str = "reels") -> str:
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
            f'<span class="rank">{int(r["rank"])}</span>'
            f'<a href="{r["url"]}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{_esc(_caption_summary(r.get("caption")))}</a>'
            f'<span class="rmeta">{" · ".join(bits)}</span>'
            "</li>"
        )
    return _card(
        f"📸 인스타그램 인기 릴스 톱 {len(cards)}",
        '<ol class="reels">' + "".join(cards) + "</ol>",
        sid,
    )


def _nav_html(items: list[tuple[str, str]]) -> str:
    if not items:
        return ""
    first = True
    out = []
    for sid, label in items:
        cls = ' class="on"' if first else ""
        first = False
        out.append(f'<li><a href="#{sid}"{cls}><span class="dot"></span>{_esc(label)}</a></li>')
    return '<ul class="nav">' + "".join(out) + "</ul>"


def html_document(
    markdown_text: str, title: str, generated_at: str = "", result: dict[str, Any] | None = None
) -> str:
    """웹페이지용 완결 HTML 문서 (GitHub Pages 게시용) — 밝은 대시보드 레이아웃.

    좌측 섹션 내비 + 상단 KPI 카드 + 섹션별 흰 카드로 구성한다. 본문은 마크다운을
    카드 단위로 쪼개 그리고(_body_cards_html), result를 주면 급상승 영상 갤러리 ·
    인기 릴스 · SVG 추이 차트를 카드로 덧붙인다 — iframe·SVG는 이메일 클라이언트 지원이
    불안정하므로(Gmail은 제거함) 웹 문서에만 넣는다.

    result가 없으면(focus 대시보드) KPI 카드와 리치 섹션을 생략하고 본문 카드만 그린다.
    """
    doc_title, lead_html, cards, nav, notes_html = _body_cards_html(markdown_text)
    extras: list[tuple[str, str, str]] = []
    if result:
        extras = [
            ("reels", "인기 릴스", _reels_html(result.get("instagram_reels"))),
            ("videos", "급상승 영상", _youtube_gallery_html(result.get("youtube_top_videos"))),
            ("charts", "주간 추이", _charts_html(result)),
        ]
    nav = nav + [(sid, label) for sid, label, html in extras if html]
    kpis = _kpi_cards(result) if result else ""
    head_title = doc_title or title
    stamp = f"<p>생성 시각: {_esc(generated_at)}</p>" if generated_at else ""
    foot = f"<footer>{notes_html}{stamp}</footer>" if (notes_html or stamp) else ""
    side_foot = (
        f'<div class="side-foot">생성 {_esc(generated_at)}</div>' if generated_at else ""
    )
    chip = f'<span class="chip">{_esc(generated_at)}</span>' if generated_at else ""
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n<style>{_PAGE_STYLE}</style>\n"
        "</head><body>\n"
        '<div class="app">\n'
        '<aside class="side">\n'
        f'<div class="brand"><span class="mark">{_icon("logo", 20)}</span>'
        f"<span><b>agape</b><span>헤어 트렌드 리포트</span></span></div>\n"
        f"{_nav_html(nav)}\n{side_foot}\n"
        "</aside>\n"
        '<main class="main"><div class="wrap">\n'
        f'<div class="top"><div><h1>{_esc(head_title)}</h1>'
        f'<p class="sub">네이버 · 유튜브 · 핀터레스트 · 인스타그램 통합 신호</p></div>'
        f"{chip}</div>\n"
        f"{lead_html}\n{kpis}\n{cards}\n"
        + "\n".join(html for _, _, html in extras if html)
        + f"\n{foot}\n</div></main>\n</div>\n"
        f"<script>{_PAGE_SCRIPT}</script>\n"
        "</body></html>\n"
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
