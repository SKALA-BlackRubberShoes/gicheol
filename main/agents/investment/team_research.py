"""웹 검색 결과의 실제 페이지 본문으로 창업자·팀 근거를 수집한다."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from main.agents.market.web_search import WebSearchBackend


_TEAM_WORDS = re.compile(
    r"창업|공동창업|대표|경영진|팀원|팀장|연구진|개발자|경력|이력|"
    r"founder|co-founder|ceo|cto|leadership|executive|engineer|team",
    re.IGNORECASE,
)


def _public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("공개 HTTP(S) 페이지 URL이 필요합니다.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("웹페이지의 비표준 포트는 허용하지 않습니다.")
    addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("공개 IP 주소로 연결되는 URL만 허용합니다.")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _public_url(newurl))


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.paragraphs: list[str] = []
        self._capture: str | None = None
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self._skip += 1
        if not self._skip and tag in {"title", "p", "li", "h1", "h2", "h3"} and self._capture is None:
            self._capture = tag
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture and not self._skip:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._capture:
            value = " ".join(" ".join(self._parts).split())
            if value:
                if tag == "title":
                    self.title = value
                else:
                    self.paragraphs.append(value)
            self._capture = None
            self._parts = []
        if tag in {"script", "style", "nav", "footer"} and self._skip:
            self._skip -= 1


def fetch_team_page(url: str) -> tuple[str, list[str]]:
    """HTML 원문만 읽는다. 검색 모델의 요약 문장은 근거로 사용하지 않는다."""
    opener = build_opener(_SafeRedirect())
    request = Request(_public_url(url), headers={"User-Agent": "Mozilla/5.0 TeamResearch/1.0"})
    with opener.open(request, timeout=12) as response:
        _public_url(response.geturl())
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("HTML 본문이 아닌 페이지입니다.")
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("본문이 너무 큽니다.")
    page = _PageText()
    page.feed(body.decode(charset, errors="replace"))
    return page.title, page.paragraphs


class TeamWebResearcher:
    """검색 URL을 직접 열어 회사와 팀에 관한 원문 단락만 근거로 등록한다."""

    def __init__(
        self, search: WebSearchBackend, *,
        fetch_page: Callable[[str], tuple[str, list[str]]] = fetch_team_page,
        max_pages: int = 5,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages는 1 이상이어야 합니다.")
        self.search = search
        self.fetch_page = fetch_page
        self.max_pages = max_pages

    def __call__(self, request: dict) -> dict:
        company = request["company"]
        company_id, name = company["id"], company["name"]
        homepage = (company.get("base_rag") or {}).get("raw", {}).get("홈페이지", "")
        home_host = (urlparse(homepage).hostname or "").removeprefix("www.").lower()
        evidence = []
        seen: set[str] = set()

        def collect(hits) -> None:
            for hit in hits:
                url = hit.url
                if url in seen:
                    continue
                seen.add(url)
                if len(evidence) >= self.max_pages:
                    break
                try:
                    title, paragraphs = self.fetch_page(url)
                except (OSError, ValueError, UnicodeError, LookupError):
                    continue
                host = (urlparse(url).hostname or "").removeprefix("www.").lower()
                company_site = bool(home_host and (host == home_host or host.endswith("." + home_host)))
                if company_site and sum(item["source_type"] == "company" for item in evidence) >= 2:
                    continue
                relevant = [part for part in paragraphs if _TEAM_WORDS.search(part)]
                if not relevant:
                    continue
                if not company_site and name.casefold() not in " ".join([title, *relevant]).casefold():
                    continue
                excerpt = "\n".join(relevant[:5])[:1800].strip()
                if not excerpt:
                    continue
                digest = sha256(url.encode("utf-8")).hexdigest()[:12]
                evidence.append({
                    "id": f"team-web-{company_id}-{digest}",
                    "document_id": url,
                    "chunk_id": digest,
                    "company_id": company_id,
                    "title": title or hit.title,
                    "publisher": host,
                    "url": url,
                    "published_at": hit.published_at,
                    "data_year": None,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "page": None,
                    "excerpt": excerpt,
                    "source_type": "company" if company_site else "independent",
                })

        collect(self.search.search(request["queries"], max_results_per_query=3))
        if not any(item["source_type"] == "independent" for item in evidence) and len(evidence) < self.max_pages:
            collect(self.search.search([
                f'"{name}" 창업자 대표 인터뷰 보도 기사 경력 -site:{home_host}'
                if home_host else f'"{name}" 창업자 대표 인터뷰 보도 기사 경력',
                f'"{name}" 로봇 AI 핵심 팀 외부 기관 발표 고객 사례',
            ], max_results_per_query=3))

        return {
            "team_info": {
                "external_research": [
                    {"evidence_id": item["id"], "title": item["title"], "url": item["url"]}
                    for item in evidence
                ],
                "evidence_ids": [item["id"] for item in evidence],
            },
            "evidence": evidence,
            "missing_items": [] if evidence else ["창업자·팀: 회사와 연결되는 공개 원문 자료를 찾지 못함"],
            "conflicts": [],
        }
