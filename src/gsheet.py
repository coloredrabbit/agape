"""Google Sheets 적재/조회 — Apps Script 웹앱과 POST로 통신한다.

서비스 계정/OAuth 대신 이 방식을 쓰는 이유: RS256 JWT 서명에 암호화 라이브러리가 필요해
의존성이 늘어나는데(CLAUDE.md: httpx/duckdb/pyyaml/dotenv 4개 유지), Apps Script 웹앱은
httpx POST 하나로 끝난다. 시트 소유자 권한으로 실행되므로 시트 공유 설정도 불필요하다.
조회(fetch)도 GET이 아니라 POST를 쓴다 — doGet은 시크릿이 URL 쿼리에 남기 때문.

시트 쪽 준비(사용자):
  스프레드시트 → 확장 프로그램 → Apps Script → 아래 코드 붙여넣기 → 배포(웹 앱,
  실행: 나, 액세스: 링크가 있는 모든 사용자) → URL을 GSHEET_WEBHOOK_URL에 저장.
  코드 수정 후에는 반드시 "새 배포"(버전 갱신)를 해야 반영된다.

  const SECRET = '아무 긴 문자열';  // GSHEET_WEBHOOK_SECRET과 동일하게

  function json_(o) {
    return ContentService.createTextOutput(JSON.stringify(o))
      .setMimeType(ContentService.MimeType.JSON);
  }

  function doPost(e) {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) return json_({ok: false, error: 'forbidden'});
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sh = ss.getSheetByName(body.sheet) || ss.insertSheet(body.sheet);

    // 조회: 전체 행을 헤더 기준 객체 배열로 반환
    if (body.action === 'fetch') {
      if (sh.getLastRow() < 2) return json_({ok: true, rows: []});
      const values = sh.getDataRange().getValues();
      const header = values.shift();
      const rows = values.map(r =>
        Object.fromEntries(header.map((h, i) => [h, r[i]])));
      return json_({ok: true, rows: rows});
    }

    // 헤더 동기화: 열 구성이 바뀌면 시트를 비우고 새 헤더로 다시 시작한다.
    // (매 실행마다 전체 데이터를 보내므로 같은 호출에서 곧바로 복구된다. 이 처리가 없으면
    //  옛 헤더 아래 새 열 데이터가 들어가 값이 다른 열로 밀린다.)
    if (body.header && body.header.length) {
      const cur = sh.getLastRow() > 0
        ? sh.getRange(1, 1, 1, body.header.length).getValues()[0]
        : [];
      const same = cur.length === body.header.length &&
        body.header.every((h, i) => String(cur[i]) === String(h));
      if (!same) {
        sh.clear();
        sh.getRange(1, 1, 1, body.header.length).setValues([body.header]);
      }
    }
    const header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    const keyIdx = header.indexOf(body.key_column || 'key');
    if (keyIdx < 0) return json_({ok: false, error: 'key column not found'});

    const rowByKey = {};
    if (sh.getLastRow() > 1) {
      sh.getRange(2, keyIdx + 1, sh.getLastRow() - 1, 1).getValues()
        .forEach((r, i) => { if (r[0] !== '') rowByKey[String(r[0])] = i + 2; });
    }
    let updated = 0;
    const toAppend = [];
    (body.rows || []).forEach(row => {
      const at = rowByKey[String(row[keyIdx])];
      if (at) { sh.getRange(at, 1, 1, row.length).setValues([row]); updated++; }
      else toAppend.push(row);
    });
    if (toAppend.length) {
      sh.getRange(sh.getLastRow() + 1, 1, toAppend.length, toAppend[0].length)
        .setValues(toAppend);
    }
    return json_({ok: true, added: toAppend.length, updated: updated});
  }
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Sequence

import httpx

from .config import GSHEET_WEBHOOK_SECRET, GSHEET_WEBHOOK_URL

RETRIES = 4  # 총 시도 횟수 (2·4·8초 백오프)


def _json_default(v: Any) -> str:
    """DuckDB가 돌려주는 datetime/date 등 비-JSON 타입을 문자열로 강제.

    httpx의 json= 인자는 기본 json.dumps를 써서 datetime에서 TypeError가 난다 —
    여기서 직접 직렬화해 모든 호출자를 한 번에 보호한다.
    """
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


class _Transient(Exception):
    """재시도로 흡수할 일시적 실패 (HTTP 오류·타임아웃·JSON 아닌 응답)."""


def _call(payload: dict[str, Any]) -> dict[str, Any]:
    """웹앱 호출 공통부 — 응답이 JSON 계약(ok/...)을 지키는지 검증한다.

    Apps Script 웹앱은 /exec → script.googleusercontent.com 리다이렉트를 거치는 구조라,
    배포가 정상이어도 간헐적으로 404·읽기 타임아웃·JSON 대신 HTML 래퍼 페이지가 돌아온다
    (모두 실측). 세 경우 모두 같은 요청을 다시 보내면 성공하므로 재시도로 흡수하고,
    끝까지 실패했을 때만 설정 문제로 안내한다. upsert는 key 기준이라 중복 전송돼도
    행이 늘지 않으므로 재시도가 안전하다(멱등).
    """
    if not GSHEET_WEBHOOK_URL:
        raise SystemExit("GSHEET_WEBHOOK_URL이 설정되지 않았습니다 (.env 확인)")
    payload = {"secret": GSHEET_WEBHOOK_SECRET, **payload}
    body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")

    for attempt in range(1, RETRIES + 1):
        try:
            resp = httpx.post(
                GSHEET_WEBHOOK_URL,
                content=body,
                headers={"Content-Type": "application/json"},
                timeout=90,
                follow_redirects=True,
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                raise _Transient(f"JSON 아님(ct={resp.headers.get('content-type','?')[:24]})")
            # 스크립트가 실제로 실행됐다 — 여기서부터는 재시도 대상이 아니다
            if not data.get("ok"):
                raise SystemExit(
                    f"Apps Script가 실패를 반환했습니다: {data.get('error') or data} "
                    "— GSHEET_WEBHOOK_SECRET 일치 여부와 스크립트 배포 버전을 확인하세요"
                )
            return data
        except (httpx.HTTPStatusError, httpx.TransportError, _Transient) as e:
            label = e.args[0] if isinstance(e, _Transient) else type(e).__name__
            if attempt == RETRIES:
                raise SystemExit(
                    f"Apps Script 호출이 {RETRIES}회 모두 실패했습니다 (마지막: {label}) — "
                    "URL이 /exec로 끝나는지, 배포 액세스가 '모든 사용자'인지 확인하세요"
                ) from e
            wait = 2 ** attempt
            print(f"[gsheet] 일시 실패({label}) — {wait}초 후 재시도 {attempt}/{RETRIES - 1}")
            time.sleep(wait)
    raise AssertionError("unreachable")  # 루프는 return 또는 raise로만 끝난다


def upsert_rows(
    sheet: str, header: Sequence[str], rows: Sequence[Sequence[Any]],
    key_column: str = "key",
) -> dict[str, int]:
    """key 열 기준 upsert — 이미 있는 게시물은 행을 갱신(view 수 최신화), 새 것은 추가."""
    if not rows:
        print(f"[gsheet] '{sheet}': 보낼 행이 없습니다")
        return {"added": 0, "updated": 0}
    data = _call({
        "sheet": sheet,
        "header": list(header),
        "rows": [list(r) for r in rows],
        "key_column": key_column,
    })
    result = {"added": int(data.get("added", 0)), "updated": int(data.get("updated", 0))}
    print(f"[gsheet] '{sheet}': 신규 {result['added']}행 · 갱신 {result['updated']}행")
    return result


def fetch_rows(sheet: str) -> list[dict[str, Any]]:
    """시트 전체 행을 헤더 기준 dict 목록으로 조회."""
    data = _call({"sheet": sheet, "action": "fetch"})
    rows = data.get("rows") or []
    print(f"[gsheet] '{sheet}': {len(rows)}행 조회")
    return rows


def sheet_safe(v: Any) -> Any:
    """수식 인젝션 방어 — 셀 값이 =, +, @, 탭으로 시작하면 아포스트로피를 붙여 텍스트로 고정.

    캡션은 외부인이 쓰는 값이라 '=IMPORTXML(...)' 같은 문자열이 그대로 셀에 들어가면
    시트를 여는 순간 수식으로 실행된다(setValues도 수식 해석을 함).
    """
    if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t"):
        return "'" + v
    return v
