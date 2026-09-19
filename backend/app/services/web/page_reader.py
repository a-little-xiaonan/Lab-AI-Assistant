"""受限网页读取：固定已校验 IP、逐跳重定向检查、只提取有限文本。"""
from __future__ import annotations

import gzip
import http.client
import re
import socket
import ssl
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO

from app.config import settings
from app.services.web.url_policy import SafeUrl, UrlPolicyError, redirected_url, validate_public_url


class PageReadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class WebPage:
    url: str
    title: str
    text: str
    content_type: str
    truncated: bool


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._title = False

    def handle_starttag(self, tag, attrs) -> None:
        self._skip += tag in {"script", "style", "noscript", "svg"}
        self._title = self._title or tag == "title"

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._title = False

    def handle_data(self, data) -> None:
        if self._skip:
            return
        if self._title:
            self.title.append(data)
        self.parts.append(data)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, target: SafeUrl, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout, context=ssl.create_default_context())
        self._target = target

    def connect(self) -> None:
        sock = socket.create_connection((self._target.ip, self._target.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._target.host)


def _decode(data: bytes, encoding: str, limit: int) -> bytes:
    try:
        if encoding in {"", "identity"}:
            out = data
        elif encoding == "gzip":
            out = gzip.GzipFile(fileobj=BytesIO(data)).read(limit + 1)
        elif encoding == "deflate":
            out = zlib.decompress(data)
        else:
            raise PageReadError("unsupported_content_encoding", "网页使用了不支持的内容编码。")
    except (OSError, zlib.error) as exc:
        raise PageReadError("invalid_content_encoding", "网页内容无法安全解码。") from exc
    if len(out) > limit:
        raise PageReadError("response_too_large", "网页内容超过读取上限。")
    return out


class PageReader:
    def __init__(self, *, timeout: float | None = None, max_bytes: int | None = None,
                 max_chars: int | None = None) -> None:
        self.timeout = timeout or settings.web_page_timeout_seconds
        self.max_bytes = max_bytes or settings.web_max_response_bytes
        self.max_chars = max_chars or settings.web_page_max_chars

    def _fetch(self, target: SafeUrl):
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = _PinnedHTTPS(target, self.timeout)
        else:
            connection = http.client.HTTPConnection(target.ip, target.port, timeout=self.timeout)
        host = target.host
        try:
            connection.request("GET", target.path_and_query, headers={
                "Host": host, "User-Agent": "Lab-AI-Assistant/1.0", "Accept": "text/html,text/plain",
            })
            response = connection.getresponse()
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise PageReadError("response_too_large", "网页内容超过读取上限。")
            return response.status, dict(response.getheaders()), body
        except PageReadError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise PageReadError("web_fetch_failed", "网页暂时无法读取。") from exc
        finally:
            connection.close()

    def read(self, url: str) -> WebPage:
        try:
            target = validate_public_url(url)
            for _ in range(settings.web_max_redirects + 1):
                status, headers, body = self._fetch(target)
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("Location") or headers.get("location")
                    if not location:
                        raise PageReadError("invalid_redirect", "网页重定向地址无效。")
                    target = redirected_url(target, location)
                    continue
                content_type = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
                if not (content_type.startswith("text/html") or content_type.startswith("text/plain")):
                    raise PageReadError("unsupported_content_type", "只支持读取公开 HTML 或纯文本网页。")
                raw = _decode(body, (headers.get("Content-Encoding") or "").lower(), self.max_bytes)
                charset = re.search(r"charset=([\\w.-]+)", content_type)
                text = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
                if content_type.startswith("text/html"):
                    parser = _Extractor()
                    parser.feed(text)
                    title, text = " ".join(parser.title), " ".join(parser.parts)
                else:
                    title = ""
                text = re.sub(r"\\s+", " ", text).strip()
                truncated = len(text) > self.max_chars
                return WebPage(target.url, re.sub(r"\\s+", " ", title).strip()[:500],
                               text[:self.max_chars], content_type, truncated)
            raise PageReadError("too_many_redirects", "网页重定向次数超过上限。")
        except UrlPolicyError as exc:
            raise PageReadError("url_not_allowed", str(exc)) from exc
