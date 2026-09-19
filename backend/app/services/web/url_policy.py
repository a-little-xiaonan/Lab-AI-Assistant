"""网页读取的 URL 策略：解析并固定到已校验的公网 IP，逐跳检查重定向。"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit


class UrlPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SafeUrl:
    url: str
    scheme: str
    host: str
    port: int
    ip: str
    path_and_query: str


def _public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def validate_public_url(url: str, *, resolve=socket.getaddrinfo) -> SafeUrl:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UrlPolicyError("只允许不含凭据的公开 HTTP(S) 地址。")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlPolicyError("URL 端口无效。") from exc
    if port not in {80, 443}:
        raise UrlPolicyError("只允许标准 HTTP(S) 端口。")
    try:
        addresses = resolve(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UrlPolicyError("无法解析网页地址。") from exc
    ips = sorted({entry[4][0] for entry in addresses if _public_ip(entry[4][0])})
    if not ips:
        raise UrlPolicyError("网页地址未解析到允许访问的公网 IP。")
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    normalised = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    return SafeUrl(normalised, parsed.scheme, parsed.hostname, port, ips[0], path)


def redirected_url(current: SafeUrl, location: str, *, resolve=socket.getaddrinfo) -> SafeUrl:
    return validate_public_url(urljoin(current.url, location), resolve=resolve)
