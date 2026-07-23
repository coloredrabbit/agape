"""크롤링 대상 사이트의 robots.txt 준수 가드.

모든 크롤링 수집기는 요청 전에 RobotsGuard.ensure_allowed(url)를 호출해야 한다.
공식 API 호출(네이버 데이터랩, YouTube Data API)은 크롤링이 아니므로 대상이 아니다.

판정은 RFC 9309를 따른다:
- 2xx + robots 형식     → 파싱 후 우리 UA 기준 can_fetch로 판정
- 4xx (404 포함)        → robots.txt 없음 = 제한 없음
- 5xx / 네트워크 오류    → 전체 비허용으로 간주하고 수집을 중단 (fail-closed)
- 2xx인데 HTML 등 비정상 → robots.txt 미제공으로 간주 (SPA의 커스텀 404 페이지 대응)
"""
from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


class RobotsDisallowed(Exception):
    """robots.txt가 해당 URL의 수집을 금지하거나, robots.txt 확인 자체가 불가한 경우."""


_ALLOW_ALL = "ALLOW_ALL"


def _looks_like_robots(body: str) -> bool:
    lowered = body.lower()
    return "user-agent" in lowered or "disallow" in lowered or "allow" in lowered


class RobotsGuard:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | str] = {}

    def _load(self, origin: str) -> RobotFileParser | str:
        robots_url = f"{origin}/robots.txt"
        try:
            resp = httpx.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=15,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise RobotsDisallowed(
                f"{robots_url} 확인 실패({e!r}) — RFC 9309에 따라 수집을 중단합니다"
            ) from e

        if 400 <= resp.status_code < 500:
            print(f"[robots] {origin}: robots.txt 없음({resp.status_code}) — 제한 없음")
            return _ALLOW_ALL
        if resp.status_code >= 500:
            raise RobotsDisallowed(
                f"{robots_url} 서버 오류({resp.status_code}) — RFC 9309에 따라 수집을 중단합니다"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/plain" not in content_type and not _looks_like_robots(resp.text):
            print(f"[robots] {origin}: robots.txt 형식 아님({content_type or '?'}) — 미제공으로 간주")
            return _ALLOW_ALL

        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        print(f"[robots] {origin}: robots.txt 파싱 완료 — UA '{self.user_agent}' 기준으로 준수")
        return parser

    def ensure_allowed(self, url: str) -> None:
        """url 수집이 robots.txt상 허용되는지 확인. 위반이면 RobotsDisallowed."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)

        state = self._cache[origin]
        if state == _ALLOW_ALL:
            return
        assert isinstance(state, RobotFileParser)
        if not state.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt가 수집을 금지: {url} (UA={self.user_agent})")
