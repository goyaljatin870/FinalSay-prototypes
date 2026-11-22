"""Public, rate-limited adapters for the supported official notice sources.

Only explicitly supported institutional hosts can be fetched.
Configured URLs, every redirect and every discovered document are checked
against the source adapter's host allowlist and public-IP policy to limit SSRF.
"""

from __future__ import annotations

import html
import hashlib
import ipaddress
import json
import re
import socket
import ssl
from threading import Event, Thread
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from finalsay.adapters.base import InstitutionAdapter, RawNotice

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_HTML_BYTES = 20_000_000
MAX_JSON_BYTES = 3_000_000
MAX_PDF_BYTES = 20_000_000
MAX_NOTICES_PER_SOURCE = 250
MAX_CATALOG_SNAPSHOT_PAGES = 500
CATALOG_SNAPSHOT_PAGES_PER_POLL = 5
MAX_REQUEST_ATTEMPTS = 3
MIN_REQUEST_INTERVAL = 0.5
MAX_RESPONSE_SECONDS = 45
RESPONSE_CHUNK_BYTES = 64 * 1024
DEFAULT_REQUEST_TIMEOUT = 45
DNS_LOOKUP_TIMEOUT_SECONDS = 5
DNS_LOOKUP_ATTEMPTS = 3
NITJ_HOSTS = {"nitj.ac.in", "www.nitj.ac.in", "v1.nitj.ac.in"}
IITB_HOSTS = {"acad.iitb.ac.in"}
VTU_HOSTS = {"vtu.ac.in", "www.vtu.ac.in"}
DU_HOSTS = {"exam.du.ac.in"}
MUMBAI_HOSTS = {"mu.ac.in", "www.mu.ac.in"}
_PUBLICATION_DATE_RE = re.compile(
    r"\b(?:publication\s+date|published(?:\s+on)?|issued\s+on|date\s+of\s+issue|"
    r"notice\s+date|dated|date)\s*[:\-]?\s*"
    r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|"
    r"\d{1,2}\s*(?:st|nd|rd|th)?[./ -][A-Za-z]{3,9}\.?[.,/ -]\d{2,4}|"
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}\s*(?:st|nd|rd|th)?,?\s+\d{4})",
    re.IGNORECASE,
)
_LISTING_DATE_RE = re.compile(
    r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}(?:,\s+\d{1,2}:\d{2}\s*[AP]M)?)\b",
    re.IGNORECASE,
)


def _site_domain(host: str) -> str:
    return host.removeprefix("www.").removeprefix("v1.")


class SourceFetchError(RuntimeError):
    """A configured source could not be fetched safely or reliably."""


class SourceNotModified(RuntimeError):
    """The source confirmed that a previously fetched document is unchanged."""


def _read_bounded_response(response, max_bytes: int, *, clock=time.monotonic) -> bytes:
    """Read a response with both a byte cap and an overall wall-clock bound."""
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise SourceFetchError(f"response exceeded {max_bytes} bytes")
        except ValueError:
            pass

    deadline = clock() + MAX_RESPONSE_SECONDS
    chunks: list[bytes] = []
    total = 0
    socket_file = getattr(response, "fp", None)
    raw_socket = getattr(getattr(socket_file, "raw", None), "_sock", None)
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SourceFetchError(
                f"response exceeded {MAX_RESPONSE_SECONDS}-second time limit"
            )
        if raw_socket is not None:
            # urllib can leave a closed socket object attached to a response
            # whose body is already buffered (for example after a server
            # sends ``Connection: close``).  Calling settimeout() on that
            # object raises EBADF before we can consume the buffered body.
            # Keep the socket timeout for live connections, but treat a
            # closed/invalid socket as already detached from network I/O.
            try:
                if raw_socket.fileno() >= 0:
                    raw_socket.settimeout(min(45.0, remaining))
            except (OSError, ValueError):
                pass
        chunk = response.read(min(RESPONSE_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise SourceFetchError(f"response exceeded {max_bytes} bytes")
    return b"".join(chunks)


def _host_is_public(host: str) -> bool:
    if not host or host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        pass
    answer = None
    last_error: OSError | None = None
    for attempt in range(DNS_LOOKUP_ATTEMPTS):
        resolved = Event()
        result: list[object] = []

        def resolve() -> None:
            try:
                result.append(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
            except OSError as exc:
                result.append(exc)
            finally:
                resolved.set()

        # getaddrinfo has no portable timeout parameter. Resolve on a daemon
        # thread so an unavailable DNS resolver cannot stall API startup.
        Thread(target=resolve, name=f"finalsay-dns-{host}", daemon=True).start()
        if not resolved.wait(DNS_LOOKUP_TIMEOUT_SECONDS):
            raise SourceFetchError(
                f"DNS lookup timed out for configured source host {host}"
            )
        answer = result[0]
        if not isinstance(answer, OSError):
            last_error = None
            break
        last_error = answer
        if attempt + 1 < DNS_LOOKUP_ATTEMPTS:
            time.sleep(0.25 * (2**attempt))
    if last_error is not None:
        raise SourceFetchError(
            f"could not resolve configured source host {host}: {last_error}"
        ) from last_error
    assert answer is not None
    addresses = {item[4][0] for item in answer}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        return False
    return True


def validate_source_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise SourceFetchError("source URL must be an HTTPS URL without embedded credentials")
    if parsed.port not in (None, 443):
        raise SourceFetchError("nonstandard source URL ports are not permitted")
    if host not in allowed_hosts:
        raise SourceFetchError("source host is not in this adapter's official-host allowlist")
    if not _host_is_public(host):
        raise SourceFetchError("source host did not resolve exclusively to public IP addresses")
    return host


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class PublicSiteClient:
    """Small bounded HTTPS client that refuses unsafe hosts and redirects."""

    def __init__(self, allowed_hosts: set[str], user_agent: str = USER_AGENT):
        self.allowed_hosts = allowed_hosts
        self.user_agent = user_agent
        self._robots: dict[str, tuple[bool, object | None]] = {}
        self._last_request: dict[str, float] = {}
        self._opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
        )

    def _wait_for_host(self, host: str) -> None:
        now = time.monotonic()
        previous = self._last_request.get(host)
        if previous is not None and now - previous < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - (now - previous))
        self._last_request[host] = time.monotonic()

    def _single_get(self, url: str, max_bytes: int, request_headers: dict[str, str] | None = None):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/json,application/pdf,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        }
        headers.update(request_headers or {})
        request = Request(
            url,
            headers=headers,
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=MAX_RESPONSE_SECONDS)
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read(4096)
        except (URLError, TimeoutError, OSError) as exc:
            raise SourceFetchError(f"request failed: {exc}") from exc
        with response:
            try:
                body = _read_bounded_response(response, max_bytes)
            except SourceFetchError:
                # Size and overall-time bounds are deterministic safeguards;
                # retrying them would only repeat the same unsafe download.
                raise
            except (URLError, TimeoutError, OSError) as exc:
                # A connection can fail after headers arrive (including
                # Errno 9 from a closed/invalid response socket). Normalize
                # stream failures so the bounded retry policy handles them
                # like failures raised while opening the connection.
                raise SourceFetchError(
                    f"request failed while reading response: {exc}"
                ) from exc
            return response.status, response.headers, body

    def _get_with_retries(
        self,
        url: str,
        max_bytes: int,
        host: str,
        request_headers: dict[str, str] | None = None,
    ):
        """Retry transient transport failures and rate/server errors twice."""
        transient_statuses = {429, 500, 502, 503, 504}
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            self._wait_for_host(host)
            try:
                if request_headers is None:
                    status, headers, body = self._single_get(url, max_bytes)
                else:
                    status, headers, body = self._single_get(
                        url, max_bytes, request_headers
                    )
            except SourceFetchError as exc:
                # Size-limit and validation errors are deterministic and must
                # not trigger another download.
                if not str(exc).startswith("request failed") or attempt + 1 == MAX_REQUEST_ATTEMPTS:
                    raise
                delay = min(2.0, 0.5 * (2**attempt))
                time.sleep(delay)
                continue
            if status not in transient_statuses or attempt + 1 == MAX_REQUEST_ATTEMPTS:
                return status, headers, body
            retry_after = headers.get("Retry-After", "")
            try:
                delay = min(5.0, max(0.0, float(retry_after)))
            except (TypeError, ValueError):
                delay = min(2.0, 0.5 * (2**attempt))
            time.sleep(delay)
        raise AssertionError("retry loop exited unexpectedly")

    def _robots_allows(self, url: str, host: str) -> bool:
        cached = self._robots.get(host)
        if cached is None:
            robots_url = f"https://{host}/robots.txt"
            current = robots_url
            status, headers, body = self._get_with_retries(current, 256_000, host)
            for _ in range(3):
                if status not in (301, 302, 303, 307, 308):
                    break
                destination_url = urljoin(current, headers.get("Location", ""))
                destination = urlsplit(destination_url)
                canonical_host = _site_domain(host)
                if (
                    destination.scheme == "https"
                    and destination.hostname == canonical_host
                    and destination.path in {"", "/"}
                ):
                    # A site's robots endpoint may redirect to its homepage;
                    # that is not a published policy file.
                    status, headers, body = 200, {"Content-Type": "text/html"}, b"<html>robots endpoint redirects to homepage</html>"
                    break
                destination_host = (destination.hostname or "").lower()
                if (
                    destination.scheme != "https"
                    or destination_host not in self.allowed_hosts
                    or destination.path != "/robots.txt"
                ):
                    status = 403
                    break
                current = destination_url
                status, headers, body = self._get_with_retries(current, 256_000, destination_host)
            body_prefix = body.lstrip()[:32].lower()
            is_html = (
                "text/html" in headers.get("Content-Type", "").lower()
                or body_prefix.startswith((b"<!doctype html", b"<html"))
            )
            if status == 200 and not is_html:
                from urllib.robotparser import RobotFileParser

                parser = RobotFileParser(robots_url)
                parser.parse(body.decode("utf-8", "replace").splitlines())
                cached = (True, parser)
            elif status in (301, 302, 303, 307, 308):
                destination = urlsplit(urljoin(robots_url, headers.get("Location", "")))
                canonical_host = _site_domain(host)
                if (
                    destination.scheme == "https"
                    and destination.hostname == canonical_host
                    and destination.path in {"", "/"}
                ):
                    # NITJ redirects robots.txt to its homepage instead of
                    # publishing a policy file; no directives are available.
                    cached = (True, None)
                else:
                    cached = (False, None)
            else:
                # A missing/non-robots endpoint has no published disallow rules.
                cached = (status in (200, 404), None)
            self._robots[host] = cached
        permitted, parser = cached
        if not permitted:
            return False
        if parser is None:
            return True
        return bool(parser.can_fetch(self.user_agent.split("/", 1)[0], url))

    def get(
        self,
        url: str,
        max_bytes: int,
        *,
        check_robots: bool = True,
        request_headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
    ):
        current = url
        original_host = validate_source_url(current, self.allowed_hosts)
        if check_robots and not self._robots_allows(current, original_host):
            raise SourceFetchError("robots.txt disallows this source URL or could not be read")
        for _ in range(4):
            host = validate_source_url(current, self.allowed_hosts)
            if host != original_host and _site_domain(host) != _site_domain(original_host):
                raise SourceFetchError("cross-host redirects are not permitted")
            status, headers, body = self._get_with_retries(
                current, max_bytes, host, request_headers
            )
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    raise SourceFetchError(f"HTTP {status} redirect did not include a destination")
                current = urljoin(current, location)
                continue
            if status == 304 and allow_not_modified:
                raise SourceNotModified(current)
            if status < 200 or status >= 300:
                raise SourceFetchError(f"source returned HTTP {status}")
            return current, headers, body
        raise SourceFetchError("source exceeded the redirect limit")


class _ThaparNoticeLinks(HTMLParser):
    """Read links from the NOTICE section on Thapar's public notice pages.

    Thapar uses both heading-based ordered lists (the Dean of Academic Affairs
    page) and a plain NOTICE label followed by an unordered list (Student
    Notices). Keep the capture limited to that section so footer/navigation
    links are not treated as notices.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._heading = False
        self._heading_text: list[str] = []
        self._pending_notice_list = False
        self._list_depth = 0
        self._list_tag: str | None = None
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        attrs_map = dict(attrs)
        if tag.lower() in {"h2", "h3", "h4"}:
            self._heading = True
            self._heading_text = []
        elif tag.lower() in {"ol", "ul"} and self._pending_notice_list and self._list_depth == 0:
            self._list_depth = 1
            self._list_tag = tag.lower()
            self._pending_notice_list = False
        elif tag.lower() in {"ol", "ul"} and self._list_depth:
            self._list_depth += 1
        elif tag.lower() == "a" and self._list_depth:
            self._anchor = (attrs_map.get("href", ""), [])

    def handle_data(self, data: str):
        if self._heading:
            self._heading_text.append(data)
        # On /students/pages/student-notices the section label is a plain text
        # node (NOTICE), not an h2/h3/h4 element.
        if not self._list_depth and " ".join(data.split()).upper() in {"NOTICE", "NOTICES"}:
            self._pending_notice_list = True
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str):
        lower = tag.lower()
        if lower in {"h2", "h3", "h4"} and self._heading:
            heading = " ".join(" ".join(self._heading_text).split()).upper()
            self._pending_notice_list = heading in {"NOTICES", "NOTICE"}
            self._heading = False
        elif lower == "a" and self._anchor is not None:
            href, text = self._anchor
            title = " ".join(" ".join(text).split())
            if href and title:
                self.links.append((href, title))
            self._anchor = None
        elif lower in {"ol", "ul"} and self._list_depth:
            self._list_depth -= 1
            if self._list_depth == 0:
                self._list_tag = None


class _PdfNoticeLinks(HTMLParser):
    """Collect titled PDF links from a configured official notice page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        if tag.lower() == "a":
            self._anchor = (dict(attrs).get("href", ""), [])

    def handle_data(self, data: str):
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._anchor is not None:
            href, text = self._anchor
            title = " ".join(" ".join(text).split())
            if href and title:
                self.links.append((href, title))
            self._anchor = None


class _CatalogRows(HTMLParser):
    """Extract notice links with nearby table/article text and pagination links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, str]] = []
        self.pages: list[str] = []
        self._container: str | None = None
        self._container_depth = 0
        self._container_text: list[str] = []
        self._container_dates: list[str] = []
        self._anchors: list[tuple[str, list[str]]] = []
        self._anchor: tuple[str, list[str]] | None = None
        self._standalone_item = False

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        attrs_map = dict(attrs)
        lower = tag.lower()
        if lower in {"tr", "article"}:
            if self._container is None:
                self._container = lower
                self._container_depth = 1
                self._container_text = []
                self._container_dates = []
                self._anchors = []
            elif self._container == "article" and lower == "article":
                self._container_depth += 1
        if lower == "a":
            href = attrs_map.get("href", "").strip()
            title = (attrs_map.get("title") or attrs_map.get("aria-label") or "").strip()
            if self._container is not None:
                self._anchor = (href, [title] if title else [])
            elif href:
                rel = attrs_map.get("rel", "").lower()
                classes = attrs_map.get("class", "").lower().split()
                if "next" in rel or re.search(r"(?:/page/\d+|[?&](?:page|offset)=\d+)", href):
                    self.pages.append(href)
                elif "list-group-item" in classes:
                    self._anchor = (href, [title] if title else [])
                    self._standalone_item = True
        if self._container is not None and lower == "time":
            date_value = attrs_map.get("datetime", "").strip()
            if date_value:
                self._container_dates.append(date_value)

    def handle_data(self, data: str):
        clean = " ".join(data.split())
        if not clean:
            return
        if self._container is not None:
            self._container_text.append(clean)
        if self._anchor is not None:
            self._anchor[1].append(clean)

    def handle_endtag(self, tag: str):
        lower = tag.lower()
        if lower == "a" and self._anchor is not None:
            href, title_parts = self._anchor
            title = " ".join(" ".join(title_parts).split())
            if href and self._standalone_item and self._container is None:
                self.items.append((href, title, title))
            elif href:
                self._anchors.append((href, title))
            self._anchor = None
            self._standalone_item = False
        if self._container == lower and lower in {"tr", "article"}:
            self._container_depth -= 1
            if self._container_depth <= 0:
                text = " ".join(" ".join(self._container_text + self._container_dates).split())
                for href, title in self._anchors:
                    self.items.append((href, title or text[:500], text[:2_000]))
                self._container = None
                self._container_text = []
                self._container_dates = []
                self._anchors = []


class _VtuAttachments(HTMLParser):
    """Collect only links in VTU's post-attachments list, ignoring site-menu PDFs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._inside = False
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        attrs_map = dict(attrs)
        if tag.lower() == "ul" and "post-attachments" in attrs_map.get("class", "").split():
            self._inside = True
        elif self._inside and tag.lower() == "a":
            self._anchor = (attrs_map.get("href", ""), [])

    def handle_data(self, data: str):
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._anchor is not None:
            href, text = self._anchor
            title = " ".join(" ".join(text).split())
            if href:
                self.links.append((href, title))
            self._anchor = None
        elif tag.lower() == "ul" and self._inside:
            self._inside = False


class _MumbaiCircularRows(HTMLParser):
    """Extract structured circular-table fields from Mumbai's public register."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, str, str, str]] = []
        self._in_row = False
        self._in_cell = False
        self._cells: list[list[str]] = []
        self._current_cell: list[str] = []
        self._anchor: tuple[str, list[str]] | None = None
        self._row_links: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        attrs_map = dict(attrs)
        lower = tag.lower()
        if lower == "tr":
            self._in_row = True
            self._cells = []
            self._row_links = []
        elif lower == "td" and self._in_row:
            self._in_cell = True
            self._current_cell = []
        elif lower == "a" and self._in_row:
            self._anchor = (attrs_map.get("href", ""), [])

    def handle_data(self, data: str):
        clean = " ".join(data.split())
        if self._in_cell and clean:
            self._current_cell.append(clean)
        if self._anchor is not None:
            self._anchor[1].append(clean)

    def handle_endtag(self, tag: str):
        lower = tag.lower()
        if lower == "a" and self._anchor is not None:
            href, _text = self._anchor
            if href:
                self._row_links.append(href)
            self._anchor = None
        elif lower == "td" and self._in_cell:
            self._cells.append(self._current_cell)
            self._in_cell = False
        elif lower == "tr" and self._in_row:
            cells = [" ".join(cell).strip() for cell in self._cells]
            if len(cells) >= 5 and len(self._row_links) >= 1:
                title, code, date = cells[2], cells[3], cells[4]
                if code and title and code.lower() not in {"unique code", "code"}:
                    department = cells[1] if len(cells) > 1 else ""
                    self.items.append((self._row_links[-1], title, code, date, department))
            self._in_row = False


def _listing_date(text: str) -> str | None:
    """Extract a date displayed as catalog metadata rather than notice content."""
    explicit = _publication_date(text)
    if explicit:
        return explicit
    matches = list(_LISTING_DATE_RE.finditer(text[:2_000]))
    if not matches:
        return None
    # DU notice rows often include the deadline in the title and the portal's
    # actual posted timestamp at the end. Prefer that timestamp so a deadline
    # is not displayed or stored as the notice's publication date.
    timestamps = [
        match for match in matches
        if re.search(r",\s*\d{1,2}:\d{2}\s*[AP]M\b", match.group(1), re.IGNORECASE)
    ]
    selected = timestamps[-1] if timestamps else matches[0]
    return " ".join(selected.group(1).split())


def _pdf_text(data: bytes) -> str:
    from finalsay.services.extraction import text_from_pdf

    return text_from_pdf(data)[:30_000]


def _publication_date(text: str) -> str | None:
    """Extract an explicitly labelled publication/issue date, not event dates."""
    match = _PUBLICATION_DATE_RE.search(text[:2_000])
    return " ".join(match.group(1).split()) if match else None


class _LiveAdapter(InstitutionAdapter):
    slug: str
    name: str
    base_url: str
    source_url: str
    allowed_hosts: set[str]
    errors: list[str]

    def __init__(self, slug: str, name: str, source_url: str, allowed_hosts: set[str]):
        self.slug = slug
        self.name = name
        self.source_url = source_url
        self.base_url = f"https://{next(iter(allowed_hosts))}"
        self.allowed_hosts = allowed_hosts
        self.errors = []
        self.not_modified_count = 0
        self.backfill_cursor: str | None = None
        self.backfill_complete = False
        self.backfill_next_cursor: str | None = None
        self.source_snapshot_keys: set[str] = set()
        self.source_snapshot_complete = False
        self.snapshot_cursor: str | None = None
        self.source_snapshot_cursor: str | None = None
        self._drive_access_token: str | None = None
        self.document_validators: dict[str, dict[str, str | None]] = {}
        self.client = PublicSiteClient(allowed_hosts)

    @staticmethod
    def _drive_file_id(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "drive.google.com" or parsed.username or parsed.password:
            return None
        match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]+)/view/?", parsed.path)
        return match.group(1) if match else None

    def _drive_pdf(self, url: str, title: str, date: str | None = None):
        """Download a catalog-linked Drive PDF through Google's read-only API."""
        file_id = self._drive_file_id(url)
        if self.slug != "university-of-delhi" or not file_id:
            return None
        from finalsay.config import get_settings

        credential_file = get_settings().google_drive_service_account_file
        if not credential_file:
            self.errors.append(
                "Google Drive attachment needs FINALSAY_GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE "
                "with read access to the official file; catalog metadata retained"
            )
            return None
        try:
            if self._drive_access_token is None:
                from google.auth.transport.requests import Request
                from google.oauth2.service_account import Credentials

                credentials = Credentials.from_service_account_file(
                    credential_file,
                    scopes=["https://www.googleapis.com/auth/drive.readonly"],
                )
                credentials.refresh(Request())
                self._drive_access_token = credentials.token

            import requests

            target = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            headers = {"Authorization": f"Bearer {self._drive_access_token}"}
            validators = self.document_validators.get(url, {})
            if validators.get("etag"):
                headers["If-None-Match"] = validators["etag"]
            for attempt in range(4):
                try:
                    response = requests.get(
                        target,
                        headers=headers if attempt == 0 else {},
                        stream=True,
                        allow_redirects=False,
                        timeout=(8, 15),
                    )
                except Exception as exc:
                    if attempt < 2:
                        time.sleep(0.5 * (2**attempt))
                        continue
                    raise SourceFetchError(
                        f"Google Drive request failed: {type(exc).__name__}: {str(exc)[:120]}"
                    ) from exc
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    response.close()
                    if not location or attempt >= 3:
                        raise SourceFetchError("Google Drive media redirect was invalid")
                    target = urljoin(target, location)
                    redirect = urlsplit(target)
                    host = (redirect.hostname or "").lower()
                    is_google_media_host = (
                        host == "drive.usercontent.google.com"
                        or host.endswith(".googleusercontent.com")
                    )
                    if redirect.scheme != "https" or not is_google_media_host:
                        raise SourceFetchError("Google Drive redirected outside its media hosts")
                    if not _host_is_public(host):
                        raise SourceFetchError("Google Drive media host did not resolve publicly")
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    response.close()
                    time.sleep(0.5 * (2**attempt))
                    continue
                if response.status_code == 304 and validators:
                    response.close()
                    return None
                if response.status_code != 200:
                    status = response.status_code
                    response.close()
                    if status in {401, 403}:
                        raise SourceFetchError(
                            "Google Drive denied read access; share the official PDF with the "
                            "configured service account or make it publicly readable"
                        )
                    if status == 404:
                        raise SourceFetchError(
                            "Google Drive returned HTTP 404 for the linked official PDF"
                        )
                    raise SourceFetchError(f"Google Drive API returned HTTP {status}")
                with response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    source_etag = response.headers.get("ETag")
                    source_last_modified = response.headers.get("Last-Modified")
                    length = response.headers.get("Content-Length")
                    if length and int(length) > MAX_PDF_BYTES:
                        raise SourceFetchError(f"response exceeded {MAX_PDF_BYTES} bytes")
                    chunks: list[bytes] = []
                    size = 0
                    deadline = time.monotonic() + MAX_RESPONSE_SECONDS
                    for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES):
                        if not chunk:
                            continue
                        if time.monotonic() >= deadline:
                            raise SourceFetchError(
                                f"response exceeded {MAX_RESPONSE_SECONDS}-second time limit"
                            )
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise SourceFetchError(f"response exceeded {MAX_PDF_BYTES} bytes")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                if not data or ("pdf" not in content_type and not data.startswith(b"%PDF")):
                    raise SourceFetchError("Google Drive response was not a PDF")
                body = _pdf_text(data)
                if not body:
                    self.errors.append(f"PDF had no extractable text: {url[:200]}")
                publication_date = date or _publication_date(body)
                fields = [f"Issued by: {self.name}", f"Subject: {title}"]
                if body:
                    fields.append(body)
                return RawNotice(
                    text="\\n".join(fields),
                    source_url=url,
                    institution_slug=self.slug,
                    external_id=url,
                    metadata={
                        "title": title[:500],
                        "source_date": publication_date,
                        "attachment_url": url,
                        "extraction_quality": "extracted" if body else "unextractable",
                        "source_content_sha256": hashlib.sha256(data).hexdigest(),
                        "source_etag": source_etag,
                        "source_last_modified": source_last_modified,
                    },
                )
            raise SourceFetchError("Google Drive media redirect limit exceeded")
        except SourceFetchError as exc:
            self.errors.append(f"{exc}: {url[:200]}")
            return None
        except Exception as exc:  # one inaccessible Drive file must not stop its source
            self.errors.append(
                f"Google Drive PDF retrieval failed ({type(exc).__name__}): {str(exc)[:140]}"
            )
            return None

    def _document(self, url: str, title: str, date: str | None = None, external_id: str | None = None):
        try:
            validators = self.document_validators.get(url, {})
            conditional_headers = {}
            if validators.get("etag"):
                conditional_headers["If-None-Match"] = validators["etag"]
            if validators.get("last_modified"):
                conditional_headers["If-Modified-Since"] = validators["last_modified"]
            _final_url, headers, data = self.client.get(
                url,
                MAX_PDF_BYTES,
                request_headers=conditional_headers,
                allow_not_modified=bool(conditional_headers),
            )
        except SourceNotModified:
            self.not_modified_count += 1
            return None
        except Exception as exc:  # noqa: BLE001 - one bad attachment must not stop its source
            self.errors.append(
                f"{type(exc).__name__}: {str(exc)[:180]} for attachment {url[:240]}"
            )
            return None
        try:
            content_type = headers.get("Content-Type", "").lower()
            path = urlsplit(url).path.lower()
            is_pdf = "pdf" in content_type or path.endswith(".pdf")
            image_suffixes = (".jpg", ".jpeg", ".png")
            is_image = path.endswith(image_suffixes) and (
                content_type.startswith("image/") or "octet-stream" in content_type
            )
            if not is_pdf and not is_image:
                self.errors.append(f"skipped unsupported notice attachment: {url}")
                return None
            if is_image:
                from finalsay.services.extraction import text_from_image

                body = text_from_image(data, timeout=8)
            else:
                body = _pdf_text(data)
            if not body:
                kind = "image" if is_image else "PDF"
                self.errors.append(f"{kind} had no extractable text: {url}")
            publication_date = date or _publication_date(body)
            fields = [f"Issued by: {self.name}", f"Subject: {title}"]
            if body:
                fields.append(body)
            return RawNotice(
                text="\n".join(fields),
                source_url=_final_url,
                institution_slug=self.slug,
                external_id=external_id or _final_url,
                metadata={
                    "title": title[:500],
                    "source_date": publication_date,
                    "extraction_quality": "extracted" if body else "unextractable",
                    "source_content_sha256": hashlib.sha256(data).hexdigest(),
                    "attachment_url": url,
                    "source_etag": headers.get("ETag"),
                    "source_last_modified": headers.get("Last-Modified"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop a source
            self.errors.append(
                f"{type(exc).__name__}: {str(exc)[:180]} for attachment {url[:240]}"
            )
            return None


class ThaparNoticeAdapter(_LiveAdapter):
    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        self.skipped_external_links: list[str] = []
        self.not_modified_count = 0
        self.source_listing_count = 0
        final_url, _headers, body = self.client.get(self.source_url, MAX_HTML_BYTES)
        parser = _ThaparNoticeLinks()
        parser.feed(body.decode("utf-8", "replace"))
        self.source_snapshot_keys = set()
        self.source_snapshot_complete = False
        notices = []
        for href, title in parser.links[:MAX_NOTICES_PER_SOURCE]:
            href = quote(html.unescape(href).strip(), safe="/%:@?&=+#")
            url = urljoin(final_url, href)
            if urlsplit(url).hostname not in self.allowed_hosts:
                # External short links are intentionally not followed: they
                # are outside this source's host allowlist, and skipping one
                # should not mark the successfully fetched first-party notice
                # list as a failed poll.
                self.skipped_external_links.append(url)
                continue
            if not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue
            self.source_listing_count += 1
            self.source_snapshot_keys.add(url)
            notice = self._document(url, title, external_id=url)
            if notice is not None:
                notices.append(notice)
        self.source_snapshot_complete = bool(parser.links) and len(parser.links) <= MAX_NOTICES_PER_SOURCE
        return notices


class NitjAcademicAdapter(_LiveAdapter):
    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        _final_url, _headers, body = self.client.get(self.source_url, MAX_JSON_BYTES)
        try:
            entries = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceFetchError(f"NITJ notice API returned invalid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise SourceFetchError("NITJ notice API response was not a list")
        self.source_snapshot_keys = set()
        self.source_snapshot_complete = False
        notices = []
        for entry in entries[:MAX_NOTICES_PER_SOURCE]:
            if not isinstance(entry, dict):
                continue
            title = html.unescape(str(entry.get("title") or "")).strip()
            raw_url = html.unescape(str(entry.get("url") or "")).strip()
            if not title or not raw_url:
                continue
            url = urljoin(self.source_url, raw_url)
            if urlsplit(url).hostname not in self.allowed_hosts:
                self.errors.append(f"skipped cross-host notice link: {url}")
                continue
            external_id = str(entry.get("ref_no") or url)
            self.source_snapshot_keys.add(external_id)
            date = str(entry.get("date") or "").strip() or None
            notice = self._document(
                url,
                title,
                date=date,
                external_id=external_id,
            )
            if notice is not None:
                notices.append(notice)
        self.source_snapshot_complete = bool(entries) and len(entries) <= MAX_NOTICES_PER_SOURCE
        return notices


class IitbAcademicAdapter(_LiveAdapter):
    """Fetch configured IIT Bombay Academic Office pages' same-site PDFs."""

    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        final_url, _headers, body = self.client.get(self.source_url, MAX_HTML_BYTES)
        parser = _PdfNoticeLinks()
        parser.feed(body.decode("utf-8", "replace"))
        self.source_snapshot_keys = set()
        self.source_snapshot_complete = False
        notices = []
        for href, title in parser.links:
            if len(notices) >= MAX_NOTICES_PER_SOURCE:
                break
            url = urljoin(final_url, quote(html.unescape(href).strip(), safe="/%:@?&=+#"))
            if not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue
            if urlsplit(url).hostname not in IITB_HOSTS:
                self.errors.append(f"skipped cross-host notice link: {url}")
                continue
            self.source_snapshot_keys.add(url)
            notice = self._document(url, title, external_id=url)
            if notice is not None:
                notices.append(notice)
        self.source_snapshot_complete = bool(parser.links) and len(parser.links) <= MAX_NOTICES_PER_SOURCE
        return notices


class _PaginatedCatalogAdapter(_LiveAdapter):
    """Read official listing pages, follow bounded same-site pagination, and ingest PDFs.

    Some university portals keep attachments on third-party file hosts. Those
    documents are deliberately not fetched: the official listing title/date
    and outbound URL are still ingested, and a partial-source error is logged.
    This avoids broadening the download allowlist beyond the institution.
    """

    supports_backfill = True
    include_all_catalog_rows = False

    @staticmethod
    def _page_index(url: str) -> tuple[str, int] | None:
        parsed = urlsplit(url)
        match = re.search(r"/page/(\d+)/?", parsed.path)
        if match:
            return ("path", int(match.group(1)))
        offset = parse_qs(parsed.query).get("offset")
        if offset and offset[0].isdigit():
            return ("offset", int(offset[0]))
        return None

    def _next_page(self, current_url: str, candidates: list[str]) -> str | None:
        current = self._page_index(current_url)
        normalized: list[tuple[int, str]] = []
        fallback: list[str] = []
        for href in candidates:
            candidate = urljoin(current_url, html.unescape(href))
            if urlsplit(candidate).hostname not in self.allowed_hosts or candidate == current_url:
                continue
            candidate_index = self._page_index(candidate)
            if current is None and candidate_index is not None:
                if candidate_index[0] == "offset" and candidate_index[1] > 0:
                    normalized.append((candidate_index[1], candidate))
                elif candidate_index[0] == "path" and candidate_index[1] > 1:
                    normalized.append((candidate_index[1], candidate))
            elif (
                current is not None
                and candidate_index is not None
                and candidate_index[0] == current[0]
                and candidate_index[1] > current[1]
            ):
                normalized.append((candidate_index[1], candidate))
            elif (
                current is not None
                and candidate_index is not None
                and candidate_index[0] == current[0]
            ):
                # A catalog page may include a previous-page link alongside
                # its next link. Never treat a same-kind cursor at or behind
                # the page we just fetched as a fallback; doing so rewinds
                # persistent backfill and can poll the same pages forever.
                continue
            else:
                fallback.append(candidate)
        if normalized:
            current = self._page_index(current_url)
            page_size = getattr(self, "backfill_page_size", None)
            if current is not None and current[0] == "offset" and page_size:
                available_offsets = {offset for offset, _url in normalized}
                next_offset = current[1] + int(page_size)
                furthest_offset = max(available_offsets)
                if next_offset < furthest_offset and next_offset not in available_offsets:
                    parsed_current = urlsplit(current_url)
                    query = parse_qs(parsed_current.query, keep_blank_values=True)
                    query["offset"] = [str(next_offset)]
                    return urlunsplit((
                        parsed_current.scheme,
                        parsed_current.netloc,
                        parsed_current.path,
                        urlencode(query, doseq=True),
                        parsed_current.fragment,
                    ))
            return min(normalized)[1]
        return fallback[0] if fallback else None

    def _catalog_urls(self, start_url: str, first_html: bytes) -> list[tuple[str, bytes]]:
        """Fetch the current page and a bounded number of backfill pages."""
        pages = [(start_url, first_html)]
        first_parser = _CatalogRows()
        first_parser.feed(first_html.decode("utf-8", "replace"))
        if self.backfill_complete:
            self.backfill_next_cursor = None
            return pages

        cursor = self.backfill_cursor or self._next_page(start_url, first_parser.pages)
        if not cursor:
            self.backfill_next_cursor = None
            self.backfill_complete = True
            return pages
        page_limit = max(1, int(getattr(self, "backfill_page_limit", 1)))
        for _ in range(page_limit):
            if urlsplit(cursor).hostname not in self.allowed_hosts:
                self.errors.append("backfill cursor is outside the source host allowlist")
                self.backfill_next_cursor = cursor
                return pages
            try:
                final_url, _headers, body = self.client.get(cursor, MAX_HTML_BYTES)
            except Exception as exc:  # keep the checkpoint so this page can be retried
                self.errors.append(
                    f"backfill page failed for {cursor}: {type(exc).__name__}: {str(exc)[:180]}"
                )
                self.backfill_next_cursor = cursor
                return pages
            pages.append((final_url, body))
            page_parser = _CatalogRows()
            page_parser.feed(body.decode("utf-8", "replace"))
            cursor = self._next_page(final_url, page_parser.pages)
            self.backfill_next_cursor = cursor
            if cursor is None:
                break
        self.backfill_complete = self.backfill_next_cursor is None
        return pages

    def _complete_catalog_snapshot(self, start_url: str, first_html: bytes) -> None:
        """Collect listing keys in bounded, resumable slices.

        A removal is recorded only after all slices are accumulated and the
        catalog crawl reaches its end. A normal hourly poll reads at most five
        pages here rather than walking hundreds of pages on every run.
        """
        self.source_snapshot_keys = set()
        self.source_snapshot_complete = False
        self.source_snapshot_cursor = None
        current_url = self.snapshot_cursor or start_url
        body = first_html
        if self.snapshot_cursor:
            try:
                current_url, _headers, body = self.client.get(
                    self.snapshot_cursor, MAX_HTML_BYTES
                )
            except Exception as exc:  # keep the checkpoint so this slice retries
                self.errors.append(
                    f"catalog snapshot page failed for {self.snapshot_cursor}: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )
                self.source_snapshot_cursor = self.snapshot_cursor
                return
        visited: set[str] = set()
        for _page_number in range(CATALOG_SNAPSHOT_PAGES_PER_POLL):
            if current_url in visited:
                self.errors.append("catalog snapshot stopped at a repeated pagination URL")
                return
            visited.add(current_url)
            parser = _CatalogRows()
            parser.feed(body.decode("utf-8", "replace"))
            for href, title, _row_text in parser.items:
                notice_url = urljoin(
                    current_url,
                    quote(html.unescape(href).strip(), safe="/%:@?&=+#"),
                )
                parsed = urlsplit(notice_url)
                if parsed.hostname not in self.allowed_hosts:
                    # Delhi's official catalog links some notices to Drive.
                    # Index that URL as a catalog key without requesting it;
                    # the fetch path still enforces the host allowlist.
                    if self.include_all_catalog_rows:
                        self.source_snapshot_keys.add(notice_url)
                    continue
                if self.slug == "vtu":
                    if parsed.fragment:
                        notice_url = parsed._replace(fragment="").geturl()
                        parsed = urlsplit(notice_url)
                    if parsed.path.lower().endswith(".pdf") or re.search(
                        r"/\d{4}/\d{1,2}/\d+/?$", parsed.path
                    ):
                        self.source_snapshot_keys.add(notice_url)
                    continue
                looks_like_document = (
                    parsed.path.lower().endswith(".pdf")
                    or "/file/d/" in parsed.path.lower()
                    or any(token in parsed.path.lower() for token in ("attachment", "download", "view"))
                    or any(token in title.lower() for token in ("pdf", "download", "circular"))
                )
                if looks_like_document or self.include_all_catalog_rows:
                    self.source_snapshot_keys.add(notice_url)

            next_url = self._next_page(current_url, parser.pages)
            if next_url is None:
                self.source_snapshot_complete = True
                self.source_snapshot_cursor = None
                return
            if urlsplit(next_url).hostname not in self.allowed_hosts:
                self.errors.append("catalog snapshot pagination left the source host allowlist")
                return
            if _page_number + 1 >= CATALOG_SNAPSHOT_PAGES_PER_POLL:
                self.source_snapshot_cursor = next_url
                return
            try:
                current_url, _headers, body = self.client.get(next_url, MAX_HTML_BYTES)
            except Exception as exc:  # noqa: BLE001 - retain all records, but do not infer removals
                self.errors.append(
                    f"catalog snapshot page failed for {next_url}: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )
                self.source_snapshot_cursor = next_url
                return
        self.errors.append(
            f"catalog snapshot exceeded the {MAX_CATALOG_SNAPSHOT_PAGES}-page safety limit"
        )

    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        final_url, _headers, first_html = self.client.get(self.source_url, MAX_HTML_BYTES)
        self._complete_catalog_snapshot(final_url, first_html)
        pages = self._catalog_urls(final_url, first_html)
        records: dict[str, RawNotice] = {}
        notice_limit = getattr(self, "notice_limit", MAX_NOTICES_PER_SOURCE)
        for page_url, body in pages:
            parser = _CatalogRows()
            parser.feed(body.decode("utf-8", "replace"))
            for href, link_title, row_text in parser.items:
                document_url = urljoin(page_url, quote(html.unescape(href).strip(), safe="/%:@?&=+#"))
                parsed = urlsplit(document_url)
                looks_like_document = (
                    parsed.path.lower().endswith(".pdf")
                    or "/file/d/" in parsed.path.lower()
                    or any(token in parsed.path.lower() for token in ("attachment", "download", "view"))
                    or any(token in link_title.lower() for token in ("pdf", "download", "circular"))
                )
                # Delhi's official notifications catalog contains authoritative
                # notice rows whose links are not consistently named as PDFs
                # or downloads. Keep those rows as listing-only evidence rather
                # than silently dropping them from the source corpus.
                if not looks_like_document and not self.include_all_catalog_rows:
                    continue
                if len(records) >= notice_limit and document_url not in records:
                    self.errors.append(f"notice cap reached ({notice_limit})")
                    break
                title = (link_title or "").strip()
                if title.lower() in {"pdf", "download", "view", "click here", "read more"}:
                    title = row_text
                title = (title or row_text or "Official notice").strip()[:500]
                publication_date = _listing_date(row_text)
                if publication_date and self.slug == "university-of-delhi":
                    title = _LISTING_DATE_RE.sub("", title).strip(" ,|-")
                    title = re.sub(r"\s+(?:STUDENT|RESULT|REVALUATION)\s*$", "", title, flags=re.IGNORECASE)
                    title = title or "University of Delhi examination notification"
                if parsed.hostname in self.allowed_hosts and looks_like_document:
                    notice = self._document(document_url, title, date=publication_date, external_id=document_url)
                else:
                    notice = self._drive_pdf(document_url, title, date=publication_date)
                    if parsed.hostname not in self.allowed_hosts and notice is None and not self._drive_file_id(document_url):
                        self.errors.append(
                            f"unsupported external attachment host; indexed catalog metadata only: {document_url[:200]}"
                        )
                if notice is None:
                    # Keep notices whose official catalog links to a broken PDF,
                    # but report reduced extraction in the source run.
                    text = f"Issued by: {self.name}\nSubject: {title}\nOfficial listing: {row_text[:2000]}"
                    notice = RawNotice(
                        text=text,
                        source_url=document_url,
                        institution_slug=self.slug,
                        external_id=document_url,
                        metadata={
                            "title": title,
                            "source_date": publication_date,
                            "attachment_url": document_url,
                            "extraction_quality": "listing_only",
                        },
                    )
                records[document_url] = notice
        return list(records.values())


class VtuExaminationAdapter(_PaginatedCatalogAdapter):
    """VTU's public WordPress Examination category and PDF attachments."""

    # Keep the latest page responsive while advancing older backfill gradually.
    notice_limit = 10
    backfill_notice_limit = 5

    def fetch_post(self, post_url: str, title: str, listing_text: str = "") -> RawNotice:
        """Fetch one known VTU post and extract only its official attachments."""
        parsed = urlsplit(post_url)
        if parsed.scheme != "https" or parsed.hostname not in VTU_HOSTS:
            raise SourceFetchError("VTU post must use HTTPS on the official VTU host")
        detail_url, _headers, detail_body = self.client.get(post_url, MAX_HTML_BYTES)
        publication_date = _listing_date(listing_text)
        attachments = _VtuAttachments()
        attachments.feed(detail_body.decode("utf-8", "replace"))
        texts: list[str] = []
        extraction_qualities: list[str] = []
        source_file: str | None = None
        source_etag: str | None = None
        source_last_modified: str | None = None
        unique_attachments: dict[str, str] = {}
        for href, attachment_title in attachments.links:
            normalized = urljoin(detail_url, html.unescape(href))
            current_title = unique_attachments.get(normalized, "")
            if attachment_title.strip() or not current_title:
                unique_attachments[normalized] = attachment_title
        for attachment_url, attachment_title in list(unique_attachments.items())[:5]:
            if urlsplit(attachment_url).hostname not in VTU_HOSTS:
                self.errors.append(f"skipped cross-host VTU attachment: {attachment_url[:200]}")
                continue
            raw = self._document(
                attachment_url,
                attachment_title or title,
                publication_date,
                external_id=post_url,
            )
            if raw is not None:
                texts.append(raw.text)
                extraction_qualities.append(raw.metadata.get("extraction_quality", "unknown"))
                source_file = source_file or attachment_url
                source_etag = source_etag or raw.metadata.get("source_etag")
                source_last_modified = source_last_modified or raw.metadata.get("source_last_modified")
        if not texts:
            self.errors.append(f"VTU notice has no extractable attachment: {post_url}")
            texts = [listing_text or title]
        extraction_quality = (
            "extracted" if "extracted" in extraction_qualities
            else "unextractable" if "unextractable" in extraction_qualities
            else "listing_only"
        )
        return RawNotice(
            text=f"Issued by: {self.name}\nSubject: {title}\n" + "\n".join(texts),
            source_url=post_url,
            institution_slug=self.slug,
            external_id=post_url,
            metadata={
                "title": title[:500],
                "source_date": publication_date,
                "attachment_url": source_file,
                "source_etag": source_etag,
                "source_last_modified": source_last_modified,
                "extraction_quality": extraction_quality,
            },
        )

    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        final_url, _headers, first_html = self.client.get(self.source_url, MAX_HTML_BYTES)
        self._complete_catalog_snapshot(final_url, first_html)
        pages = self._catalog_urls(final_url, first_html)
        records: dict[str, RawNotice] = {}
        for page_index, (page_url, body) in enumerate(pages):
            listing = _CatalogRows()
            listing.feed(body.decode("utf-8", "replace"))
            page_limit = (
                self.notice_limit if page_index == 0 else self.backfill_notice_limit
            )
            processed_from_page = 0
            for href, listing_title, listing_text in listing.items:
                post_url = urljoin(page_url, html.unescape(href))
                parsed = urlsplit(post_url)
                if parsed.hostname not in VTU_HOSTS:
                    continue
                # WordPress also includes a "No Comment" link to the same
                # post with a #respond fragment. Treat it as the post URL so
                # the comment anchor cannot create a duplicate notice/version.
                if parsed.fragment:
                    post_url = parsed._replace(fragment="").geturl()
                    parsed = urlsplit(post_url)
                if parsed.path.lower().endswith(".pdf"):
                    if processed_from_page >= page_limit:
                        self.errors.append(f"notice cap reached ({page_limit})")
                        break
                    notice = self._document(
                        post_url, listing_title, _listing_date(listing_text), post_url
                    )
                    if notice is not None:
                        records[post_url] = notice
                        processed_from_page += 1
                    continue
                if not re.search(r"/\d{4}/\d{1,2}/\d+/?$", parsed.path):
                    continue
                if post_url in records:
                    continue
                if processed_from_page >= page_limit:
                    self.errors.append(f"notice cap reached ({page_limit})")
                    break
                try:
                    notice = self.fetch_post(post_url, listing_title, listing_text)
                except Exception as exc:  # one withdrawn post must not stop the catalog
                    self.errors.append(f"post fetch failed for {post_url}: {type(exc).__name__}: {str(exc)[:180]}")
                    if listing_text:
                        notice = RawNotice(
                            text=f"Issued by: {self.name}\nSubject: {listing_title}\n{listing_text}",
                            source_url=post_url,
                            institution_slug=self.slug,
                            external_id=post_url,
                            metadata={
                                "title": listing_title[:500],
                                "source_date": _listing_date(listing_text),
                                "extraction_quality": "listing_only",
                            },
                        )
                    else:
                        continue
                records[post_url] = notice
                processed_from_page += 1
        return list(records.values())


class DuExaminationAdapter(_PaginatedCatalogAdapter):
    """University of Delhi Examination portal notices and linked documents."""

    # The portal uses 20-item offsets. One current page plus five bounded
    # historical pages processes up to 120 notices per poll and shortens a
    # full replay without making a single poll unbounded.
    notice_limit = 120
    backfill_page_limit = 5
    backfill_page_size = 20
    include_all_catalog_rows = True


class MumbaiCircularAdapter(_PaginatedCatalogAdapter):
    """University of Mumbai public circular register."""

    notice_limit = 10

    def fetch(self) -> list[RawNotice]:
        self.errors.clear()
        final_url, _headers, body = self.client.get(self.source_url, MAX_HTML_BYTES)
        parser = _MumbaiCircularRows()
        parser.feed(body.decode("utf-8", "replace"))
        self.source_snapshot_keys = {f"mumbai:{item[2]}" for item in parser.items}
        # This endpoint returns the complete circular register in one table.
        # An empty parse is treated as an unknown page shape, never as proof
        # that every previously collected circular was removed.
        self.source_snapshot_complete = bool(parser.items)
        notices: list[RawNotice] = []
        recent_items = parser.items[:self.notice_limit]
        cursor = self.backfill_cursor
        backfill_start = self.notice_limit
        cursor_error = False
        if cursor and cursor.startswith("mumbai-code:"):
            code = cursor.removeprefix("mumbai-code:")
            cursor_index = next(
                (index for index, item in enumerate(parser.items) if item[2] == code),
                None,
            )
            if cursor_index is None:
                # The portal periodically removes old circular rows. A missing
                # checkpoint must not pin backfill forever: restart from the
                # first historical row still present after the latest window.
                # Stable external IDs make replay idempotent in the database.
                backfill_start = self.notice_limit
            else:
                backfill_start = cursor_index + 1
        elif cursor and cursor.isdigit():
            # Migrate previously persisted row-offset cursors once by replacing
            # them with the final code selected by this batch below.
            backfill_start = int(cursor)
        elif cursor:
            self.errors.append(f"unsupported Mumbai backfill cursor: {cursor[:80]}")
            backfill_start = len(parser.items)
            cursor_error = True
        historical_items = (
            parser.items[backfill_start : backfill_start + self.notice_limit]
            if not self.backfill_complete and not cursor_error
            else []
        )
        selected_items = recent_items + historical_items
        if cursor_error:
            self.backfill_next_cursor = cursor
        elif not self.backfill_complete and historical_items:
            last_index = backfill_start + len(historical_items) - 1
            self.backfill_next_cursor = f"mumbai-code:{historical_items[-1][2]}"
            self.backfill_complete = last_index + 1 >= len(parser.items)
            if self.backfill_complete:
                self.backfill_next_cursor = None
        elif not self.backfill_complete:
            self.backfill_next_cursor = None
            self.backfill_complete = True
        else:
            self.backfill_next_cursor = None
        for href, title, circular_code, date, department in selected_items:
            attachment = urljoin(final_url, quote(html.unescape(href).strip(), safe="/%:@?&=+#"))
            if urlsplit(attachment).hostname not in MUMBAI_HOSTS:
                self.errors.append(f"skipped cross-host Mumbai attachment: {attachment[:200]}")
                continue
            # The circular register should link documents. Never turn an image,
            # script, or other upload into a catalog-only notice: the Mumbai
            # portal has exposed rows with non-document upload names, and the
            # generic fallback below would otherwise index them as official
            # notices despite having no extractable notice document.
            path = urlsplit(attachment).path.lower()
            supported_attachment = path.endswith((".pdf", ".jpg", ".jpeg", ".png"))
            suspicious_filename_title = re.search(
                r"(?:zocext|^txt[0-9a-f]{3,}|(?:^|[_-])(?:php\d*|phtml|shtml|phar|pht)(?:[_-]|$))",
                title,
                re.IGNORECASE,
            )
            if not supported_attachment or suspicious_filename_title:
                self.errors.append(f"skipped unsupported or suspicious Mumbai attachment: {attachment[:200]}")
                continue
            publication_date = _listing_date(date)
            raw = self._document(
                attachment,
                title,
                date=publication_date,
                external_id=f"mumbai:{circular_code}",
            )
            if raw is None:
                listing = (
                    f"Issued by: {self.name}\nSubject: {title}\n"
                    f"Department: {department}\nCircular code: {circular_code}\n"
                    f"Publication date: {date}"
                )
                raw = RawNotice(
                    text=listing,
                    source_url=attachment,
                    institution_slug=self.slug,
                    external_id=f"mumbai:{circular_code}",
                    metadata={
                        "title": title[:500],
                        "source_date": publication_date,
                        "attachment_url": attachment,
                        "extraction_quality": "listing_only",
                    },
                )
            else:
                raw.text = (
                    f"Issued by: {self.name}\nSubject: {title}\n"
                    f"Department: {department}\nCircular code: {circular_code}\n"
                    f"Publication date: {date}\n{raw.text}"
                )
                raw.metadata["title"] = title[:500]
                raw.metadata["source_date"] = publication_date
            notices.append(raw)
        return notices


def validate_scrape_config(
    scrape_type: str, source_url: str | None, *, resolve_host: bool = True
) -> None:
    allowed = {
        "thapar_html": {"www.thapar.edu"},
        "nitj_api": NITJ_HOSTS,
        "iitb_html": IITB_HOSTS,
        "vtu_examination": VTU_HOSTS,
        "du_examination": DU_HOSTS,
        "mumbai_circulars": MUMBAI_HOSTS,
    }
    if scrape_type not in allowed:
        raise SourceFetchError(f"unsupported scraper type: {scrape_type}")
    if not source_url:
        raise SourceFetchError("a public source URL is required for a live scraper")
    if resolve_host:
        validate_source_url(source_url, allowed[scrape_type])
    else:
        parsed = urlsplit(source_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or parsed.port not in (None, 443) or host not in allowed[scrape_type]:
            raise SourceFetchError("source URL must use HTTPS on this adapter's official host")


def create_live_adapter(institution) -> InstitutionAdapter:  # noqa: ANN001
    scraper = institution.scrape_type
    source_url = institution.source_url or ""
    validate_scrape_config(scraper, source_url)
    if scraper == "thapar_html":
        return ThaparNoticeAdapter(
            institution.slug,
            institution.name,
            source_url,
            {"www.thapar.edu"},
        )
    if scraper == "nitj_api":
        return NitjAcademicAdapter(
            institution.slug,
            institution.name,
            source_url,
            NITJ_HOSTS,
        )
    if scraper == "iitb_html":
        return IitbAcademicAdapter(
            institution.slug,
            institution.name,
            source_url,
            IITB_HOSTS,
        )
    if scraper == "vtu_examination":
        return VtuExaminationAdapter(institution.slug, institution.name, source_url, VTU_HOSTS)
    if scraper == "du_examination":
        return DuExaminationAdapter(institution.slug, institution.name, source_url, DU_HOSTS)
    if scraper == "mumbai_circulars":
        return MumbaiCircularAdapter(institution.slug, institution.name, source_url, MUMBAI_HOSTS)
    raise SourceFetchError(f"unsupported live scraper type: {scraper}")

