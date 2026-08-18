from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from .config import RAW_DIR, STATE_DIR


def write_jsonl(source: str, rows: list[dict[str, Any]], run_date: date | None = None) -> Path:
    """실행일별 JSONL로 저장하되, 같은 실행일 파일이 이미 있으면 기존 행을 보존해 병합한다.

    같은 날 collect가 두 번 돌아도(일요일 CI의 daily+weekly, 수동 재실행 등) 먼저 쓴
    데이터 — 특히 최초/필터변경 시의 대용량 백필 — 가 사라지지 않게 한다. 예전에는 "w"로
    덮어써서 백필 직후의 30일 증분 재수집이 2년치를 통째로 날렸다. 병합으로 쌓인 중복은
    읽기 시점에 (논리키, date)별 fetched_at 최신본으로 dedupe되므로(집계 쿼리의 ROW_NUMBER)
    안전하다 — 이 멱등성이 성립하려면 raw JSONL을 읽는 모든 집계가 그 dedupe를 거쳐야 한다
    (인기차트처럼 '스냅샷 전체가 한 덩어리'인 소스는 행별 최신본이 아니라 최신 스냅샷으로
    고정해야 한다. metrics.latest_trending_hair 참고).

    임시파일에 쓴 뒤 os.replace로 교체하므로, 한 프로세스가 쓰는 동안 죽어도 기존 파일은
    그대로 남는다(기존 "w"는 반쪽 파일을 남길 수 있었다). 단 프로세스 두 개가 같은 소스를
    동시에 쓰면 늦게 끝난 쪽이 이기고 먼저 끝난 쪽의 행은 누락된다 — 파일이 깨지지는 않지만
    원자적 read-modify-write는 아니다. CI는 concurrency 그룹으로 직렬화되고 cmd_collect도
    수집기를 순차 실행하므로 현재 경로에서는 발생하지 않는다.
    """
    run_date = run_date or date.today()
    out_dir = RAW_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_date.isoformat()}.jsonl"

    # 기존 행은 문자열 그대로 보존한다(스키마가 바뀐 과거 행도 건드리지 않기 위해).
    # 단 깨진 줄은 버린다 — 그대로 복사하면 영구히 남아 이후 모든 집계가 파싱 오류로 죽는다
    # (예전 "w" 모드는 다음 실행에 덮어써서 저절로 나았지만, 병합은 스스로 낫지 못한다).
    # errors="replace"로 읽어 잘린 멀티바이트 문자가 있어도 읽기 단계에서 막히지 않게 한다.
    # split("\n")을 쓴다 — splitlines()는 U+2028/U+2029/U+0085에서도 쪼개는데, 이 문자들은
    # JSON 문자열 안에 이스케이프 없이 들어갈 수 있어(영상 제목 등) 유효한 한 줄이 두 조각으로
    # 갈라지고 아래 검증에서 '깨진 줄'로 오판돼 삭제된다. 쓸 때 "\n"만 붙이므로 이게 정확한 역연산.
    kept: list[str] = []
    dropped = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except ValueError:
                dropped += 1
                continue
            kept.append(line)
    if dropped:
        print(f"[storage] {source}/{path.name}: 깨진 행 {dropped}개를 제외하고 병합했습니다")

    # 임시파일 이름에 PID를 넣는다 — 고정 이름이면 동시 실행되는 두 프로세스가 같은 임시파일을
    # truncate해 서로의 출력을 뒤섞고 바이트 단위로 깨진 파일을 만든다(실측). PID를 붙이면
    # 최악의 경우가 "늦은 쪽이 이김"(유효한 파일)으로 그친다. *.jsonl 글롭에도 걸리지 않는다.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # SIGKILL 등으로 try/except가 못 도는 경우 임시파일이 남는다. 읽기에는 안 걸리지만(*.jsonl
    # 글롭 밖) CI의 `git add -f data`가 커밋할 수 있어, 하루 이상 묵은 것만 청소한다
    # (진행 중인 다른 프로세스의 임시파일을 지우지 않도록 나이 기준을 둔다).
    stale_before = time.time() - 86400
    for old in out_dir.glob("*.tmp"):
        try:
            if old != tmp and old.stat().st_mtime < stale_before:
                old.unlink()
        except OSError:
            pass
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # 실패 시 임시파일을 남기지 않는다
        raise
    return path


def source_glob(source: str) -> str:
    return str(RAW_DIR / source / "*.jsonl")


def has_data(source: str) -> bool:
    """읽을 만한 데이터가 있는지. 0바이트 파일은 없는 것으로 친다.

    빈 JSONL만 있으면 DuckDB가 컬럼을 하나도 못 잡아 집계 쿼리가 BinderException으로
    죽는다(리포트 전체가 중단됨). 수집 결과가 0건인 소스는 정상적으로 생길 수 있으므로
    여기서 걸러 모든 리더를 한 번에 보호한다.
    """
    d = RAW_DIR / source
    if not d.exists():
        return False
    return any(f.stat().st_size > 0 for f in d.glob("*.jsonl"))


def load_state(name: str, default: Any) -> Any:
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(name: str, value: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
