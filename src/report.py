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
from .metrics import KeywordMetrics

TOP_N = 15


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:+.0f}%" if v is not None else "-"


def _fmt_num(v: float | None, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if v is not None else "-"


def _fmt_int(v: int | None) -> str:
    return f"{v:,}" if v is not None else "-"


def render_markdown(result: dict[str, Any]) -> str:
    week = result["week"]
    metrics: list[KeywordMetrics] = result["metrics"]
    lines = [f"# 헤어 트렌드 주간 리포트 — {week.isoformat()} 주"]

    flagged = [m for m in metrics if m.signal][:TOP_N]
    if flagged:
        lines.append("")
        lines.append(
            "| 시그널 | 키워드 | 네이버 z | 네이버 WoW | 전년비 | 유튜브 Δ뷰 | 유튜브 WoW | 핀 MoM |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for m in flagged:
            lines.append(
                f"| {m.signal} | {m.keyword} | {_fmt_num(m.naver_z)} | {_fmt_pct(m.naver_velocity)} "
                f"| {_fmt_pct(m.naver_yoy)} | {_fmt_int(m.yt_views)} | {_fmt_pct(m.yt_velocity)} "
                f"| {_fmt_pct(m.pin_mom)} |"
            )
    else:
        lines.append("")
        lines.append("이번 주 급등 시그널 없음.")

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

    if result.get("shopping"):
        lines.append("")
        lines.append("**쇼핑 클릭 (카테고리)**")
        for s in result["shopping"]:
            lines.append(f"- {s['category']}: WoW {_fmt_pct(s['velocity'])}")

    if result.get("trending"):
        lines.append("")
        lines.append("**유튜브 인기 급상승 중 헤어 관련**")
        for t in result["trending"][:5]:
            lines.append(f"- #{t['rank']} {t['title']} ({t['channel']}, {_fmt_int(t['views'])}뷰)")

    lines.append("")
    covered = sum(1 for m in metrics if m.naver_vol is not None)
    lines.append(f"_추적 키워드 {len(metrics)}개 중 네이버 데이터 확보 {covered}개_")
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
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 14px;
  display: block; overflow-x: auto; }
th, td { border: 1px solid #8884; padding: 6px 10px; text-align: left; white-space: nowrap; }
th { background: #8882; }
ul { padding-left: 1.2rem; }
footer { margin-top: 2rem; color: #8889; font-size: 12px; }
"""


def html_document(markdown_text: str, title: str, generated_at: str = "") -> str:
    """웹페이지용 완결 HTML 문서. 마크다운 리포트를 감싼다 (GitHub Pages 게시용)."""
    body = markdown_to_html(markdown_text)
    foot = f"<footer>생성 시각: {generated_at}</footer>" if generated_at else ""
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{_PAGE_STYLE}</style>\n"
        f"</head><body>\n{body}\n{foot}\n</body></html>\n"
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
