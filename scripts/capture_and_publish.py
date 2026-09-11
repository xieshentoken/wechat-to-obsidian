#!/usr/bin/env python3
"""Fetch one public HTTPS article and publish create-only Obsidian artifacts."""

from __future__ import annotations

import errno
import base64
import gzip
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import getproxies


SPEC_NAME = ".geof_cobs_task.json"
RESULT_NAME = "publish_result.json"
MAX_URL_LENGTH = 4096
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 80 * 1024 * 1024
MAX_IMAGES = 60
MAX_TABLES = 40
MAX_TABLE_ROWS = 250
MAX_TABLE_COLUMNS = 50
MAX_TABLE_CELLS = 5000
MAX_TABLE_SPAN = 100
SOCKET_TIMEOUT_SECONDS = 25
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
WECHAT_ARTICLE_MIN_CHARS = 80
XAI_ARTICLE_MIN_CHARS = 80
X_ARTICLE_MIN_CHARS = 80
MAX_X_DRAFT_BLOCKS = 1000
WECHAT_CHALLENGE_MARKERS = (
    "当前环境异常",
    "完成验证后即可继续访问",
)
ALLOWED_IMAGE_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
BLOCKED_IMAGE_HINTS = {
    "avatar",
    "qrcode",
    "qr_code",
    "spacer",
    "tracking",
    "pixel",
    "loading.gif",
    "placeholder",
}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "form"}
TEX_ENCODING_HINTS = ("tex", "latex")
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "figure",
    "figcaption",
    "footer",
    "header",
    "main",
    "nav",
    "p",
    "pre",
    "section",
    "table",
    "tr",
}
TABLE_SECTION_TAGS = {"thead", "tbody", "tfoot"}
TABLE_CELL_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "figcaption",
    "figure",
    "footer",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "pre",
    "section",
    "ul",
    "ol",
}


class CaptureError(RuntimeError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)


@dataclass
class TableCell:
    node: Node
    is_header: bool
    rowspan: int = 1
    colspan: int = 1


@dataclass
class TableRow:
    cells: list[TableCell]
    section: str = ""


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.metadata: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        normalized_attrs = {
            str(key).casefold(): str(value or "")
            for key, value in attrs
            if key
        }
        if normalized_tag == "meta":
            self.metadata.append(normalized_attrs)
        node = Node(normalized_tag, normalized_attrs)
        self.stack[-1].children.append(node)
        if normalized_tag not in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        normalized_tag = tag.casefold()
        if self.stack[-1].tag == normalized_tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == normalized_tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


class PinnedHTTPSConnection:
    """Minimal HTTPS client that connects to a prevalidated public IP."""

    def __init__(self, host: str, ip: str, port: int = 443) -> None:
        self.host = host
        self.ip = ip
        self.port = port

    def _connect_transport(self):
        proxy_value = str(getproxies().get("https") or "").strip()
        if not proxy_value:
            return socket.create_connection(
                (self.ip, self.port),
                timeout=SOCKET_TIMEOUT_SECONDS,
            )

        proxy = urlsplit(proxy_value)
        if proxy.scheme.casefold() not in {"http", "https"} or not proxy.hostname:
            raise CaptureError("sandbox HTTPS proxy configuration is invalid")
        try:
            proxy_port = proxy.port or (443 if proxy.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise CaptureError("sandbox HTTPS proxy port is invalid") from exc
        transport = socket.create_connection(
            (proxy.hostname, proxy_port),
            timeout=SOCKET_TIMEOUT_SECONDS,
        )
        if proxy.scheme.casefold() == "https":
            transport = ssl.create_default_context().wrap_socket(
                transport,
                server_hostname=proxy.hostname,
            )

        authority = f"[{self.ip}]:{self.port}" if ":" in self.ip else f"{self.ip}:{self.port}"
        connect_headers = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: keep-alive",
        ]
        if proxy.username is not None:
            username = proxy.username or ""
            password = proxy.password or ""
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            connect_headers.append(f"Proxy-Authorization: Basic {token}")
        connect_headers.extend(["", ""])
        transport.sendall("\r\n".join(connect_headers).encode("ascii"))
        response = transport.makefile("rb")
        status_line = response.readline(65537).decode("iso-8859-1").strip()
        match = re.fullmatch(r"HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+.*)?", status_line)
        if not match:
            transport.close()
            raise CaptureError("sandbox HTTPS proxy returned an invalid CONNECT response")
        while True:
            line = response.readline(65537)
            if not line or line in {b"\r\n", b"\n"}:
                break
        if int(match.group(1)) != 200:
            transport.close()
            raise CaptureError(f"sandbox HTTPS proxy refused CONNECT with HTTP {match.group(1)}")
        return transport

    def request(self, target: str, headers: dict[str, str]) -> tuple[int, Message, bytes]:
        raw_socket = self._connect_transport()
        try:
            context = ssl.create_default_context()
            tls_socket = context.wrap_socket(raw_socket, server_hostname=self.host)
            request_lines = [f"GET {target} HTTP/1.1"]
            for key, value in headers.items():
                request_lines.append(f"{key}: {value}")
            request_lines.extend(["Connection: close", "", ""])
            tls_socket.sendall("\r\n".join(request_lines).encode("utf-8"))
            response = tls_socket.makefile("rb")
            status_line = response.readline(65537).decode("iso-8859-1").strip()
            match = re.fullmatch(r"HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+.*)?", status_line)
            if not match:
                raise CaptureError("remote server returned an invalid HTTP status line")
            status = int(match.group(1))
            headers_obj = Message()
            while True:
                line = response.readline(65537)
                if not line or line in {b"\r\n", b"\n"}:
                    break
                decoded = line.decode("iso-8859-1")
                if decoded[:1] in {" ", "\t"}:
                    raise CaptureError("folded HTTP response headers are not accepted")
                name, separator, value = decoded.partition(":")
                if not separator:
                    raise CaptureError("remote server returned a malformed HTTP header")
                headers_obj[name.strip()] = value.strip()
            body = read_http_body(response, headers_obj)
            return status, headers_obj, body
        finally:
            try:
                raw_socket.close()
            except OSError:
                pass


def read_http_body(stream, headers: Message) -> bytes:
    transfer_encoding = str(headers.get("Transfer-Encoding") or "").casefold()
    if "chunked" in transfer_encoding:
        chunks = bytearray()
        while True:
            size_line = stream.readline(128).split(b";", 1)[0].strip()
            try:
                size = int(size_line, 16)
            except ValueError as exc:
                raise CaptureError("invalid chunked response") from exc
            if size == 0:
                while stream.readline(65537) not in {b"", b"\r\n", b"\n"}:
                    pass
                break
            if len(chunks) + size > MAX_HTML_BYTES + MAX_IMAGE_BYTES:
                raise CaptureError("response exceeds hard size limit")
            chunks.extend(stream.read(size))
            if stream.read(2) != b"\r\n":
                raise CaptureError("invalid chunk boundary")
        return bytes(chunks)

    length_value = headers.get("Content-Length")
    if length_value:
        try:
            length = int(length_value)
        except ValueError as exc:
            raise CaptureError("invalid Content-Length") from exc
        if length < 0 or length > MAX_HTML_BYTES + MAX_IMAGE_BYTES:
            raise CaptureError("response exceeds hard size limit")
        return stream.read(length)
    return stream.read(MAX_HTML_BYTES + MAX_IMAGE_BYTES + 1)


def normalize_public_https_url(raw_url: str) -> str:
    value = html.unescape(str(raw_url or "").strip())
    if not value or len(value) > MAX_URL_LENGTH or any(char.isspace() for char in value):
        raise CaptureError("URL must be one non-empty HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https":
        raise CaptureError("only HTTPS URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise CaptureError("URL host is missing or userinfo is forbidden")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CaptureError("URL port is invalid") from exc
    if port not in {None, 443}:
        raise CaptureError("only HTTPS port 443 is allowed")
    host = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise CaptureError("local hostnames are forbidden")
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise CaptureError("non-public destination is forbidden")
    netloc = f"[{host}]" if ":" in host else host
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def resolve_public_addresses(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CaptureError(f"DNS resolution failed for {host}") from exc
    addresses: list[str] = []
    for record in records:
        raw_ip = record[4][0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise CaptureError(f"DNS returned an invalid address for {host}") from exc
        if not address.is_global:
            raise CaptureError(f"non-public destination is forbidden: {host}")
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise CaptureError(f"DNS returned no usable address for {host}")
    return addresses


def decode_content_encoding(body: bytes, headers: Message, max_bytes: int) -> bytes:
    encoding = str(headers.get("Content-Encoding") or "").casefold().strip()
    try:
        if encoding in {"", "identity"}:
            decoded = body
        elif encoding == "gzip":
            decoded = gzip.decompress(body)
        elif encoding == "deflate":
            decoded = zlib.decompress(body)
        else:
            raise CaptureError(f"unsupported content encoding: {encoding}")
    except (OSError, zlib.error) as exc:
        raise CaptureError("response decompression failed") from exc
    if len(decoded) > max_bytes:
        raise CaptureError("decoded response exceeds size limit")
    return decoded


def pinned_https_get(
    raw_url: str,
    *,
    max_bytes: int,
    accept: str,
    referer: str = "",
) -> tuple[str, str, bytes]:
    current = normalize_public_https_url(raw_url)
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlsplit(current)
        host = parsed.hostname or ""
        addresses = resolve_public_addresses(host)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        headers = {
            "Host": host,
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": ACCEPT_LANGUAGE,
        }
        if referer:
            headers["Referer"] = normalize_public_https_url(referer)
        last_error: Exception | None = None
        for address in addresses:
            try:
                status, response_headers, body = PinnedHTTPSConnection(
                    host,
                    address,
                ).request(target, headers)
                break
            except (OSError, ssl.SSLError, CaptureError) as exc:
                last_error = exc
        else:
            raise CaptureError(f"HTTPS connection failed for {host}: {last_error}")

        if status in {301, 302, 303, 307, 308}:
            location = str(response_headers.get("Location") or "").strip()
            if not location:
                raise CaptureError("redirect is missing Location")
            current = normalize_public_https_url(urljoin(current, location))
            continue
        if status < 200 or status >= 300:
            raise CaptureError(f"remote server returned HTTP {status}")
        decoded = decode_content_encoding(body, response_headers, max_bytes)
        content_type = str(response_headers.get_content_type() or "").casefold()
        return current, content_type, decoded
    raise CaptureError("too many HTTPS redirects")


def walk_nodes(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from walk_nodes(child)


def class_tokens(node: Node) -> set[str]:
    return {token.casefold() for token in node.attrs.get("class", "").split() if token}


def raw_node_text(node: Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in SKIP_TAGS:
            parts.append(raw_node_text(child))
    return "".join(parts)


def looks_like_latex(value: str) -> bool:
    return bool(
        re.search(r"\\(?:[a-zA-Z]+|.)", value or "")
        or "^{" in value
        or "_{" in value
    )


def latex_source_from_node(node: Node) -> str:
    if node.tag == "img":
        formula = html.unescape(node.attrs.get("data-formula", "")).strip()
        if formula:
            return formula
        if "equation" in class_tokens(node):
            src = node.attrs.get("src", "").strip().casefold()
            alt = html.unescape(node.attrs.get("alt", "")).strip()
            if src.startswith("data:") and looks_like_latex(alt):
                return alt
        return ""
    if node.tag in {"annotation", "annotation-xml"}:
        encoding = node.attrs.get("encoding", "").casefold()
        if any(hint in encoding for hint in TEX_ENCODING_HINTS):
            return raw_node_text(node).strip()
        return ""
    if node.tag == "math":
        return html.unescape(node.attrs.get("alttext", "")).strip()
    return ""


def find_latex_source(node: Node) -> str:
    for current in walk_nodes(node):
        source = latex_source_from_node(current)
        if source:
            return source
    return ""


def is_display_math(node: Node) -> bool:
    style = node.attrs.get("style", "").casefold().replace(" ", "")
    return (
        "display:block" in style
        or "display:flex" in style
        or "katex-display" in class_tokens(node)
    )


def select_article_node(root: Node) -> Node:
    nodes = list(walk_nodes(root))
    for node in nodes:
        if node.attrs.get("id", "").casefold() == "js_content":
            return node
    for node in nodes:
        if "rich_media_content" in class_tokens(node):
            return node
    for node in nodes:
        if node.tag == "article":
            return node
    for node in nodes:
        if node.tag == "main":
            return node
    for node in nodes:
        if node.tag == "body":
            return node
    return root


def is_xai_news_url(raw_url: str) -> bool:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold().rstrip("/")
    return host in {"x.ai", "www.x.ai"} and (path == "/news" or path.startswith("/news/"))


def is_x_status_url(raw_url: str) -> bool:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.rstrip("/")
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and bool(
        re.fullmatch(r"/[^/]+/status/\d+", path, re.IGNORECASE)
    )


def select_xai_news_node(root: Node) -> Node | None:
    for section in walk_nodes(root):
        if section.tag != "section":
            continue
        descendants = list(walk_nodes(section))
        has_title_marker = any(
            node.tag == "h1"
            and node.attrs.get("id", "").casefold() == "post-title-navigator-anchor"
            for node in descendants
        )
        if not has_title_marker:
            continue
        for node in descendants:
            if "prose" in class_tokens(node):
                return node
    return None


def xai_news_metadata(root: Node) -> dict[str, str]:
    news_article: dict | None = None
    for node in walk_nodes(root):
        if node.tag != "script" or node.attrs.get("type", "").casefold() != "application/ld+json":
            continue
        try:
            payload = json.loads(raw_node_text(node).strip())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            candidates = [payload, *payload["@graph"]]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_types = candidate.get("@type")
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            if any(
                re.split(r"[/#]", str(value or ""))[-1].casefold() == "newsarticle"
                for value in types
            ):
                news_article = candidate
                break
        if news_article is not None:
            break
    if news_article is None:
        raise CaptureError("xAI NewsArticle metadata is missing; refusing to publish a fallback page")

    author_value = news_article.get("author")
    authors = author_value if isinstance(author_value, list) else [author_value]
    author_names: list[str] = []
    for author in authors:
        name = author.get("name") if isinstance(author, dict) else author
        normalized = normalize_inline_text(str(name or ""))
        if normalized and normalized not in author_names:
            author_names.append(normalized)

    image_value = news_article.get("image")
    image_candidates = image_value if isinstance(image_value, list) else [image_value]
    cover_url = ""
    for candidate in image_candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("contentUrl")
        if not isinstance(candidate, str):
            continue
        try:
            cover_url = normalize_public_https_url(candidate)
        except CaptureError:
            continue
        break

    return {
        "title": normalize_inline_text(str(news_article.get("headline") or "")),
        "description": normalize_inline_text(str(news_article.get("description") or "")),
        "author": ", ".join(author_names),
        "published": normalize_inline_text(str(news_article.get("datePublished") or "")),
        "cover_url": cover_url,
    }


JS_STRING_PATTERN = r'"(?:\\["\\/bfnrt]|\\u[0-9a-fA-F]{4}|[^"\\\x00-\x1f])*"'


def decode_js_string(token: str, field_name: str) -> str:
    try:
        value = json.loads(token)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"X Article {field_name} is malformed") from exc
    if not isinstance(value, str):
        raise CaptureError(f"X Article {field_name} is malformed")
    return value


def has_x_article_body(root: Node) -> bool:
    return any("x-article-body" in class_tokens(node) for node in walk_nodes(root))


def x_article_stream(root: Node) -> str:
    candidates: list[str] = []
    for node in walk_nodes(root):
        if node.tag != "script":
            continue
        value = raw_node_text(node)
        if (
            '__typename:"ArticleEntity"' in value
            and '__typename:"DraftJsContentState"' in value
        ):
            candidates.append(value)
    if not candidates:
        raise CaptureError(
            "X Article Draft.js data is missing; refusing to publish a text-only fallback"
        )
    return max(candidates, key=lambda value: (value.count("original_img_url"), len(value)))


def x_article_cover_url(root: Node, source_url: str) -> str:
    for node in walk_nodes(root):
        if node.tag != "img":
            continue
        if normalize_inline_text(node.attrs.get("alt", "")).casefold() != "article cover image":
            continue
        url = image_url_from_node(node, source_url)
        if url:
            return url
    return ""


def extract_x_article(root: Node, source_url: str) -> dict:
    if not has_x_article_body(root):
        raise CaptureError(
            "X Article body marker is missing; refusing to publish a fallback page"
        )
    stream = x_article_stream(root)

    title_match = re.search(
        rf'__typename:"ArticleEntity",title:({JS_STRING_PATTERN})',
        stream,
    )
    state_match = re.search(
        r'__id:"([^"\x00-\x1f]+:content_state)",__typename:"DraftJsContentState"',
        stream,
    )
    if not title_match or not state_match:
        raise CaptureError("X Article metadata or content state is incomplete")
    title = normalize_inline_text(decode_js_string(title_match.group(1), "title"))
    if not title:
        raise CaptureError("X Article title is missing")
    content_state_id = state_match.group(1)
    escaped_state_id = re.escape(content_state_id)

    block_pattern = re.compile(
        rf'{escaped_state_id}:blocks:(\d+)":\$R\[\d+\]=\{{'
        rf'__id:{JS_STRING_PATTERN},__typename:"DraftJsBlock",'
        rf'key:{JS_STRING_PATTERN},text:({JS_STRING_PATTERN}),type:({JS_STRING_PATTERN})'
    )
    block_matches = list(block_pattern.finditer(stream))
    if not block_matches or len(block_matches) > MAX_X_DRAFT_BLOCKS:
        raise CaptureError("X Article Draft.js block count is invalid")
    block_indexes = [int(match.group(1)) for match in block_matches]
    if block_indexes != list(range(len(block_matches))):
        raise CaptureError("X Article Draft.js block sequence is incomplete")

    entity_pattern = re.compile(
        rf'{escaped_state_id}:entity_map:(\d+)":\$R\[\d+\]=\{{'
        rf'__id:{JS_STRING_PATTERN},__typename:"DraftJsEntityMap",key:({JS_STRING_PATTERN})'
    )
    entity_matches = list(entity_pattern.finditer(stream))
    entity_media_ids: dict[int, list[str]] = {}
    for position, match in enumerate(entity_matches):
        end = entity_matches[position + 1].start() if position + 1 < len(entity_matches) else -1
        if end < 0:
            next_media = stream.find('__typename:"ApiMedia"', match.end())
            end = next_media if next_media >= 0 else min(len(stream), match.end() + 20000)
        segment = stream[match.start():end]
        if '__typename:"DraftJsEntity",type:"MEDIA"' not in segment:
            continue
        raw_key = decode_js_string(match.group(2), "entity key")
        if not raw_key.isdigit():
            raise CaptureError("X Article media entity key is malformed")
        media_ids = re.findall(
            r'__typename:"ArticleMediaKey",media_id:"(\d+)"',
            segment,
        )
        if not media_ids:
            raise CaptureError("X Article media entity has no media identifier")
        entity_media_ids[int(raw_key)] = media_ids

    media_starts = list(re.finditer(r'__typename:"ApiMedia"', stream))
    media_urls: dict[str, str] = {}
    for position, match in enumerate(media_starts):
        end = media_starts[position + 1].start() if position + 1 < len(media_starts) else len(stream)
        segment = stream[match.start():min(end, match.start() + 4096)]
        media_id_match = re.search(r'media_id:"(\d+)"', segment)
        image_match = re.search(rf'original_img_url:({JS_STRING_PATTERN})', segment)
        if not media_id_match or not image_match:
            continue
        media_urls[media_id_match.group(1)] = normalize_public_https_url(
            decode_js_string(image_match.group(1), "image URL")
        )

    images: list[dict[str, str | int]] = []
    markdown_parts: list[str] = []
    cover_url = x_article_cover_url(root, source_url)
    if cover_url:
        images.append({"index": 1, "url": cover_url})
        markdown_parts.append("{{GEOF_IMAGE_1}}")

    for position, match in enumerate(block_matches):
        block_index = int(match.group(1))
        block_text = decode_js_string(match.group(2), "block text")
        block_type = decode_js_string(match.group(3), "block type")
        end = (
            block_matches[position + 1].start()
            if position + 1 < len(block_matches)
            else entity_matches[0].start() if entity_matches else len(stream)
        )
        segment = stream[match.start():end]

        if block_type == "atomic":
            entity_keys = [
                int(value)
                for value in re.findall(
                    r'__typename:"DraftJsEntityRange",key:(\d+)',
                    segment,
                )
            ]
            if not entity_keys:
                raise CaptureError(
                    f"X Article atomic block {block_index} has no media mapping"
                )
            block_markers: list[str] = []
            for entity_key in entity_keys:
                media_ids = entity_media_ids.get(entity_key)
                if not media_ids:
                    raise CaptureError(
                        f"X Article atomic block {block_index} has an unsupported entity"
                    )
                for media_id in media_ids:
                    image_url = media_urls.get(media_id)
                    if not image_url:
                        raise CaptureError(
                            f"X Article image data is missing for media {media_id}"
                        )
                    if len(images) >= MAX_IMAGES:
                        raise CaptureError("X Article image count exceeds the safety limit")
                    image_index = len(images) + 1
                    images.append({"index": image_index, "url": image_url})
                    block_markers.append(f"{{{{GEOF_IMAGE_{image_index}}}}}")
            markdown_parts.extend(block_markers)
            continue

        text = normalize_inline_text(block_text)
        if not text:
            continue
        heading_match = re.fullmatch(r"header-([a-z]+)", block_type)
        heading_levels = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
        }
        if heading_match and heading_match.group(1) in heading_levels:
            markdown_parts.append(f"{'#' * heading_levels[heading_match.group(1)]} {text}")
        elif block_type == "unordered-list-item":
            markdown_parts.append(f"- {text}")
        elif block_type == "ordered-list-item":
            markdown_parts.append(f"1. {text}")
        elif block_type == "blockquote":
            markdown_parts.append(f"> {text}")
        elif block_type == "code-block":
            longest_fence = max(
                (len(value.group()) for value in re.finditer(r"`+", block_text)),
                default=0,
            )
            fence = "`" * max(3, longest_fence + 1)
            markdown_parts.append(f"{fence}text\n{block_text}\n{fence}")
        else:
            markdown_parts.append(text)

    markdown = "\n\n".join(markdown_parts).strip()
    if len(normalize_inline_text(markdown)) < X_ARTICLE_MIN_CHARS:
        raise CaptureError("X Article body extraction returned too little text")
    return {
        "title": title,
        "markdown": markdown,
        "images": images,
        "math_count": 0,
        "article_marker": "x:article+draftjs",
    }


def wechat_article_marker(root: Node) -> str:
    for node in walk_nodes(root):
        if node.attrs.get("id", "").casefold() == "js_content":
            return "id:js_content"
    for node in walk_nodes(root):
        if "rich_media_content" in class_tokens(node):
            return "class:rich_media_content"
    return ""


def node_text(node: Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in SKIP_TAGS:
            parts.append(node_text(child))
    return normalize_inline_text(" ".join(parts))


def metadata_value(parser: DocumentParser, *names: str) -> str:
    expected = {name.casefold() for name in names}
    for item in parser.metadata:
        key = (item.get("property") or item.get("name") or "").casefold()
        if key in expected and item.get("content"):
            return normalize_inline_text(item["content"])
    return ""


def first_node_text(root: Node, predicate) -> str:
    for node in walk_nodes(root):
        if predicate(node):
            value = node_text(node)
            if value:
                return value
    return ""


def normalize_inline_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value or ""))
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", value).strip()


def is_wechat_host(host: str) -> bool:
    normalized = str(host or "").casefold().rstrip(".")
    return normalized == "mp.weixin.qq.com" or normalized.endswith(".weixin.qq.com")


def reject_wechat_access_challenge(final_url: str, html_text: str) -> None:
    parsed = urlsplit(final_url)
    if not is_wechat_host(parsed.hostname or ""):
        return
    normalized_path = parsed.path.casefold()
    normalized_text = normalize_inline_text(html_text).casefold()
    if "appmsgcaptcha" in normalized_path or any(
        marker.casefold() in normalized_text for marker in WECHAT_CHALLENGE_MARKERS
    ):
        raise CaptureError("WeChat source redirected to an access challenge page")


def image_url_from_node(node: Node, base_url: str) -> str:
    for key in ("data-src", "data-original", "data-lazy-src", "src"):
        candidate = html.unescape(node.attrs.get(key, "")).strip()
        if not candidate or candidate.startswith(("data:", "javascript:")):
            continue
        try:
            return normalize_public_https_url(urljoin(base_url, candidate))
        except CaptureError:
            continue
    return ""


def is_decorative_image(node: Node, url: str) -> bool:
    haystack = " ".join(
        [url, node.attrs.get("alt", ""), node.attrs.get("class", ""), node.attrs.get("id", "")]
    ).casefold()
    if any(hint in haystack for hint in BLOCKED_IMAGE_HINTS):
        return True
    dimensions = []
    for key in ("width", "height"):
        match = re.match(r"\d+", node.attrs.get(key, ""))
        if match:
            dimensions.append(int(match.group()))
    return len(dimensions) == 2 and max(dimensions) <= 64


class MarkdownRenderer:
    def __init__(
        self,
        base_url: str,
        *,
        images: list[dict[str, str | int]] | None = None,
        code_blocks: list[str] | None = None,
    ) -> None:
        self.base_url = base_url
        self.parts: list[str] = []
        self.images = images if images is not None else []
        self.code_blocks = code_blocks if code_blocks is not None else []
        self.skip_depth = 0
        self.list_stack: list[str] = []
        self.table_count = 0
        self.math_count = 0

    def render(self, node: Node) -> tuple[str, list[dict[str, str | int]], int]:
        self._render_node(node)
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        for index, block in enumerate(self.code_blocks):
            text = text.replace(f"\x00GEOF_CODE_{index}\x00", block)
        return text, self.images, self.math_count

    def _append_break(self, count: int = 1) -> None:
        self.parts.append("\n" * count)

    def _register_image(self, node: Node, *, html_context: bool = False) -> str:
        url = image_url_from_node(node, self.base_url)
        if not url or is_decorative_image(node, url) or len(self.images) >= MAX_IMAGES:
            return ""
        index = len(self.images) + 1
        self.images.append({"index": index, "url": url})
        marker_kind = "GEOF_HTML_IMAGE" if html_context else "GEOF_IMAGE"
        return f"{{{{{marker_kind}_{index}}}}}"

    def _register_code_block(self, node: Node) -> str:
        code = raw_node_text(node).replace("\r\n", "\n").replace("\r", "\n")
        lines = code.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return ""
        code = "\n".join(lines)
        longest_fence = max((len(match.group()) for match in re.finditer(r"`+", code)), default=0)
        fence = "`" * max(3, longest_fence + 1)
        index = len(self.code_blocks)
        self.code_blocks.append(f"{fence}text\n{code}\n{fence}")
        return f"\x00GEOF_CODE_{index}\x00"

    def _render_math_node(self, node: Node) -> bool:
        if node.tag == "img":
            latex = latex_source_from_node(node)
            if latex:
                self._emit_math(latex, node)
                return True
            return False
        classes = class_tokens(node)
        is_math = (
            node.tag in {"math", "annotation", "annotation-xml", "mjx-container"}
            or (
                node.tag in {"span", "div"}
                and ("katex" in classes or "katex-display" in classes)
            )
        )
        if not is_math:
            return False
        latex = find_latex_source(node)
        if latex:
            self._emit_math(latex, node, default_display="katex-display" in classes)
        return True

    def _emit_math(self, latex: str, node: Node, *, default_display: bool = False) -> None:
        latex = latex.strip()
        if not latex:
            return
        display = (
            default_display
            or is_display_math(node)
            or latex.startswith(("\\begin{", "\\displaystyle"))
        )
        if display:
            self.parts.append(f"\n\n$${latex}$$\n\n")
        else:
            latex = re.sub(r"\s+", " ", latex).strip()
            if self.parts and not self.parts[-1].endswith((" ", "\n")):
                self.parts.append(" ")
            self.parts.append(f"${latex}$")
        self.math_count += 1

    @staticmethod
    def _table_span(node: Node, name: str) -> tuple[int, bool]:
        raw_value = node.attrs.get(name, "").strip()
        if not raw_value:
            return 1, True
        if not re.fullmatch(r"[1-9]\d*", raw_value):
            return 1, False
        value = int(raw_value)
        if value > MAX_TABLE_SPAN:
            raise CaptureError(f"table {name} exceeds the safety limit")
        return value, True

    @staticmethod
    def _table_row_nodes(table: Node) -> tuple[list[tuple[Node, str]], bool]:
        rows: list[tuple[Node, str]] = []
        nested_table = False

        def visit(node: Node, section: str = "") -> None:
            nonlocal nested_table
            for child in node.children:
                if not isinstance(child, Node):
                    continue
                if child.tag == "table":
                    nested_table = True
                    continue
                child_section = child.tag if child.tag in TABLE_SECTION_TAGS else section
                if child.tag == "tr":
                    rows.append((child, child_section))
                    if any(
                        descendant is not child and descendant.tag == "table"
                        for descendant in walk_nodes(child)
                    ):
                        nested_table = True
                    continue
                visit(child, child_section)

        visit(table)
        return rows, nested_table

    @staticmethod
    def _table_cell_nodes(row: Node) -> list[Node]:
        cells: list[Node] = []

        def visit(node: Node) -> None:
            for child in node.children:
                if not isinstance(child, Node):
                    continue
                if child.tag in {"table", "tr"}:
                    continue
                if child.tag in {"th", "td"}:
                    cells.append(child)
                    continue
                visit(child)

        visit(row)
        return cells

    def _parse_table(self, table: Node) -> tuple[list[TableRow], str, bool]:
        row_nodes, nested_table = self._table_row_nodes(table)
        if len(row_nodes) > MAX_TABLE_ROWS:
            raise CaptureError("table row count exceeds the safety limit")

        rows: list[TableRow] = []
        total_cells = 0
        malformed = nested_table
        for row_node, section in row_nodes:
            parsed_cells: list[TableCell] = []
            for cell_node in self._table_cell_nodes(row_node):
                rowspan, valid_rowspan = self._table_span(cell_node, "rowspan")
                colspan, valid_colspan = self._table_span(cell_node, "colspan")
                malformed = malformed or not valid_rowspan or not valid_colspan
                parsed_cells.append(TableCell(
                    node=cell_node,
                    is_header=cell_node.tag == "th",
                    rowspan=rowspan,
                    colspan=colspan,
                ))
            if not parsed_cells:
                continue
            total_cells += len(parsed_cells)
            if total_cells > MAX_TABLE_CELLS:
                raise CaptureError("table cell count exceeds the safety limit")
            effective_columns = sum(cell.colspan for cell in parsed_cells)
            if effective_columns > MAX_TABLE_COLUMNS:
                raise CaptureError("table column count exceeds the safety limit")
            rows.append(TableRow(cells=parsed_cells, section=section))

        caption = ""
        for child in table.children:
            if isinstance(child, Node) and child.tag == "caption":
                caption = node_text(child)
                break
        return rows, caption, malformed

    def _render_markdown_table_cell(self, cell: TableCell) -> str:
        text = self._render_children_to_text(cell.node)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n+", "<br>", text).strip()
        text = re.sub(r"(?<!\\)\|", r"\\|", text)
        return text or " "

    def _render_html_table_inline(self, value: Node | str) -> str:
        if isinstance(value, str):
            normalized = unicodedata.normalize("NFKC", html.unescape(value or ""))
            normalized = normalized.replace("\u00a0", " ").replace("\u200b", "")
            return html.escape(re.sub(r"\s+", " ", normalized), quote=False)
        classes = class_tokens(value)
        is_math = (
            value.tag in {"math", "annotation", "annotation-xml", "mjx-container"}
            or (value.tag == "img" and bool(latex_source_from_node(value)))
            or (
                value.tag in {"span", "div"}
                and ("katex" in classes or "katex-display" in classes)
            )
        )
        if is_math:
            latex = find_latex_source(value).strip()
            if not latex:
                return ""
            self.math_count += 1
            latex = re.sub(r"\s+", " ", latex)
            return html.escape(f"${latex}$", quote=False)
        if value.tag in SKIP_TAGS:
            return ""
        if value.tag == "img":
            return self._register_image(value, html_context=True)
        if value.tag == "br":
            return "<br>"

        content = "".join(self._render_html_table_inline(child) for child in value.children)
        if value.tag in {"strong", "b"} and content:
            return f"<strong>{content}</strong>"
        if value.tag in {"em", "i"} and content:
            return f"<em>{content}</em>"
        if value.tag == "code" and content:
            return f"<code>{content}</code>"
        if value.tag == "a":
            href = value.attrs.get("href", "").strip()
            try:
                href = normalize_public_https_url(urljoin(self.base_url, href)) if href else ""
            except CaptureError:
                href = ""
            if content and href:
                return f'<a href="{html.escape(href, quote=True)}">{content}</a>'
            return content
        if value.tag == "li" and content:
            return f"&#8226; {content}<br>"
        if value.tag in TABLE_CELL_BLOCK_TAGS and content:
            return f"{content}<br>"
        return content

    def _render_html_table_cell(self, cell: TableCell) -> str:
        content = "".join(
            self._render_html_table_inline(child) for child in cell.node.children
        )
        content = re.sub(r"(?:\s*<br>\s*)+$", "", content).strip()
        return content or "&nbsp;"

    def _render_table_fallback(
        self,
        rows: list[TableRow],
        caption: str,
    ) -> str:
        lines: list[str] = []
        if caption:
            lines.append(f"**{caption}**")
        lines.append("**表格结构异常，已按行保留：**")
        for index, row in enumerate(rows, start=1):
            values = [node_text(cell.node).replace("|", r"\|") or "（空）" for cell in row.cells]
            lines.append(f"- 第 {index} 行：" + " | ".join(values))
        return "\n\n" + "\n".join(lines) + "\n\n"

    def _render_table(self, table: Node) -> None:
        self.table_count += 1
        if self.table_count > MAX_TABLES:
            raise CaptureError("table count exceeds the safety limit")

        rows, caption, malformed = self._parse_table(table)
        if not rows:
            text = node_text(table)
            if text:
                self.parts.append(f"\n\n{text}\n\n")
            return

        widths = [sum(cell.colspan for cell in row.cells) for row in rows]
        simple_markdown = (
            not malformed
            and len(set(widths)) == 1
            and all(
                cell.rowspan == 1 and cell.colspan == 1
                for row in rows
                for cell in row.cells
            )
        )
        if simple_markdown:
            rendered = [
                [self._render_markdown_table_cell(cell) for cell in row.cells]
                for row in rows
            ]
            lines: list[str] = []
            if caption:
                lines.extend([f"**{caption}**", ""])
            lines.append("| " + " | ".join(rendered[0]) + " |")
            lines.append("| " + " | ".join("---" for _ in rendered[0]) + " |")
            lines.extend("| " + " | ".join(row) + " |" for row in rendered[1:])
            self.parts.append("\n\n" + "\n".join(lines) + "\n\n")
            return

        if malformed:
            self.parts.append(self._render_table_fallback(rows, caption))
            return

        lines = ["<table>"]
        if caption:
            lines.append(f"  <caption>{html.escape(caption, quote=False)}</caption>")
        for row in rows:
            lines.append("  <tr>")
            for cell in row.cells:
                tag = "th" if cell.is_header else "td"
                attributes = []
                if cell.rowspan > 1:
                    attributes.append(f'rowspan="{cell.rowspan}"')
                if cell.colspan > 1:
                    attributes.append(f'colspan="{cell.colspan}"')
                suffix = " " + " ".join(attributes) if attributes else ""
                content = self._render_html_table_cell(cell)
                lines.append(f"    <{tag}{suffix}>{content}</{tag}>")
            lines.append("  </tr>")
        lines.append("</table>")
        self.parts.append("\n\n" + "\n".join(lines) + "\n\n")

    def _render_node(self, node: Node) -> None:
        if node.tag in SKIP_TAGS:
            return
        if self._render_math_node(node):
            return
        if node.tag == "table":
            self._render_table(node)
            return
        if node.tag == "img":
            marker = self._register_image(node)
            if marker:
                self.parts.append(f"\n\n{marker}\n\n")
            return
        if node.tag == "br":
            self._append_break()
            return
        if node.tag == "hr":
            self.parts.append("\n\n---\n\n")
            return

        heading_match = re.fullmatch(r"h([1-6])", node.tag)
        if heading_match:
            text = node_text(node)
            if text:
                level = int(heading_match.group(1))
                self.parts.append(f"\n\n{'#' * level} {text}\n\n")
            return
        if node.tag == "li":
            text = self._render_children_to_text(node)
            if text:
                marker = "1." if self.list_stack and self.list_stack[-1] == "ol" else "-"
                self.parts.append(f"\n{marker} {text}")
            return
        if node.tag in {"ul", "ol"}:
            self.list_stack.append(node.tag)
            self._append_break()
            for child in node.children:
                if isinstance(child, Node):
                    self._render_node(child)
            self.list_stack.pop()
            self._append_break()
            return
        if node.tag == "blockquote":
            text = self._render_children_to_text(node)
            if text:
                quoted = "\n".join(f"> {line}" for line in text.splitlines() if line.strip())
                self.parts.append(f"\n\n{quoted}\n\n")
            return
        if node.tag == "pre":
            marker = self._register_code_block(node)
            if marker:
                self.parts.append(f"\n\n{marker}\n\n")
            return
        if node.tag in {"strong", "b"}:
            text = self._render_children_to_text(node)
            if text:
                self.parts.append(f"**{text}**")
            return
        if node.tag in {"em", "i"}:
            text = self._render_children_to_text(node)
            if text:
                self.parts.append(f"*{text}*")
            return
        if node.tag == "a":
            text = self._render_children_to_text(node)
            href = node.attrs.get("href", "").strip()
            try:
                href = normalize_public_https_url(urljoin(self.base_url, href)) if href else ""
            except CaptureError:
                href = ""
            self.parts.append(f"[{text}]({href})" if text and href else text)
            return

        is_block = node.tag in BLOCK_TAGS
        if is_block:
            self._append_break(2)
        for child in node.children:
            if isinstance(child, str):
                value = normalize_inline_text(child)
                if value:
                    if self.parts and not self.parts[-1].endswith((" ", "\n")):
                        self.parts.append(" ")
                    self.parts.append(value)
            else:
                self._render_node(child)
        if is_block:
            self._append_break(2)

    def _render_children_to_text(self, node: Node) -> str:
        nested = MarkdownRenderer(
            self.base_url,
            images=self.images,
            code_blocks=self.code_blocks,
        )
        nested.list_stack = list(self.list_stack)
        temporary = Node("span", children=node.children)
        nested._render_node(temporary)
        self.math_count += nested.math_count
        return re.sub(r"\n{3,}", "\n\n", "".join(nested.parts)).strip()


def decode_html_document(data: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    else:
        prefix = data[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", prefix, re.IGNORECASE)
        if match:
            charset = match.group(1)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def extract_article(html_text: str, source_url: str) -> dict:
    parser = DocumentParser()
    parser.feed(html_text)
    parser.close()
    reject_wechat_access_challenge(source_url, html_text)
    wechat_marker = wechat_article_marker(parser.root)
    is_wechat = is_wechat_host(urlsplit(source_url).hostname or "")
    is_xai_news = is_xai_news_url(source_url)
    is_x_status = is_x_status_url(source_url)
    is_x_article = is_x_status and (
        has_x_article_body(parser.root) or '__typename:"ArticleEntity"' in html_text
    )
    if is_wechat and not wechat_marker:
        raise CaptureError(
            "WeChat article marker is missing; refusing to publish a fallback page"
        )
    if is_x_article:
        article = extract_x_article(parser.root, source_url)
        social_title = metadata_value(parser, "og:title", "twitter:title")
        author = re.sub(r"\s+on X$", "", social_title, flags=re.IGNORECASE)
        published = metadata_value(parser, "article:published_time", "date", "pubdate")
        return {
            "title": normalize_inline_text(article["title"])[:180],
            "author": normalize_inline_text(author)[:180],
            "published": normalize_inline_text(published)[:80],
            "description": "",
            "markdown": article["markdown"],
            "images": article["images"],
            "math_count": article["math_count"],
            "article_marker": article["article_marker"],
        }
    xai_metadata: dict[str, str] = {}
    article_marker = wechat_marker
    if is_xai_news:
        article_node = select_xai_news_node(parser.root)
        if article_node is None:
            raise CaptureError("xAI article marker is missing; refusing to publish a fallback page")
        xai_metadata = xai_news_metadata(parser.root)
        article_marker = "xai:post-title+prose+newsarticle"
    else:
        article_node = select_article_node(parser.root)
    title = (
        xai_metadata.get("title", "")
        or metadata_value(parser, "og:title", "twitter:title")
        or first_node_text(
            parser.root,
            lambda node: node.tag == "h1" and (
                "rich_media_title" in class_tokens(node) or node.attrs.get("id") == "activity-name"
            ),
        )
        or first_node_text(parser.root, lambda node: node.tag == "title")
        or first_node_text(article_node, lambda node: node.tag == "h1")
    )
    if not title:
        if is_wechat:
            raise CaptureError("WeChat article title is missing")
        title = "未命名网页"
    author = (
        xai_metadata.get("author", "")
        or metadata_value(parser, "author", "article:author")
        or first_node_text(
            parser.root,
            lambda node: "rich_media_meta_text" in class_tokens(node)
            or node.attrs.get("id") == "js_name",
        )
    )
    published = (
        xai_metadata.get("published", "")
        or metadata_value(parser, "article:published_time", "date", "pubdate")
    )
    images: list[dict[str, str | int]] = []
    cover_url = xai_metadata.get("cover_url", "")
    if cover_url:
        images.append({"index": 1, "url": cover_url})
    markdown, images, math_count = MarkdownRenderer(source_url, images=images).render(article_node)
    minimum_chars = (
        WECHAT_ARTICLE_MIN_CHARS
        if is_wechat
        else XAI_ARTICLE_MIN_CHARS if is_xai_news else 20
    )
    if len(normalize_inline_text(markdown)) < minimum_chars:
        raise CaptureError("article body extraction returned too little text")
    if cover_url:
        markdown = "{{GEOF_IMAGE_1}}\n\n" + markdown
    return {
        "title": normalize_inline_text(title)[:180],
        "author": normalize_inline_text(author)[:180],
        "published": normalize_inline_text(published)[:80],
        "description": normalize_inline_text(xai_metadata.get("description", ""))[:500],
        "markdown": markdown,
        "images": images,
        "math_count": math_count,
        "article_marker": article_marker,
    }


def sniff_image(data: bytes, content_type: str) -> tuple[str, str]:
    declared = (content_type or "").split(";", 1)[0].strip().casefold()
    if data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected = "image/webp"
    else:
        raise CaptureError("downloaded image has an unsupported signature")
    if declared.startswith("image/") and declared not in ALLOWED_IMAGE_MIME:
        raise CaptureError(f"downloaded image MIME is forbidden: {declared}")
    return detected, ALLOWED_IMAGE_MIME[detected]


def sanitize_filename(value: str, fallback: str = "网页笔记") -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return (normalized or fallback)[:100].rstrip(" .-") or fallback


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_only_bytes(directory: Path, filename: str, data: bytes) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise CaptureError("unsafe destination filename")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    destination = directory / filename
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise CaptureError(f"cannot create destination file: {filename}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise CaptureError(
                f"failed writing and rolling back destination file: {filename}: {cleanup_error}"
            ) from cleanup_error
        raise
    return destination


def choose_create_only_name(directory: Path, stem: str, suffix: str, data: bytes) -> Path:
    candidates = [f"{stem}{suffix}"]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidates.extend(f"{stem}-{timestamp}-{index}{suffix}" for index in range(1, 1000))
    for candidate in candidates:
        try:
            return create_only_bytes(directory, candidate, data)
        except FileExistsError:
            continue
    raise CaptureError("could not allocate a collision-free destination filename")


APPROVED_VAULT_SHA256 = (
    "21ee9a529b613f2ea466e5d25350bc6df2f758ad8954e0ab06b282f5e64fb4ef"
)


def validate_destination(spec: dict, *, enforce_production: bool) -> tuple[Path, Path, Path]:
    vault = Path(str(spec.get("vault_root") or "")).expanduser().resolve()
    inbox = Path(str(spec.get("inbox_dir") or "")).expanduser().resolve()
    images = Path(str(spec.get("image_dir") or "")).expanduser().resolve()
    if enforce_production:
        # The approved production vault is pinned by the SHA-256 of its resolved
        # path, so the real deployment directory is not disclosed in the
        # published source. An empty or mismatched pin fails closed before
        # anything is written.
        if not APPROVED_VAULT_SHA256:
            raise CaptureError("no approved production vault is pinned")
        if hashlib.sha256(str(vault).encode("utf-8")).hexdigest() != APPROVED_VAULT_SHA256:
            raise CaptureError("production vault path does not match the approved vault")
    if inbox != vault / "00-Inbox" or images != vault / "06-Attachments/00-images":
        raise CaptureError("destination directories do not match the approved vault layout")
    for directory in (vault, inbox, images):
        if not directory.is_dir() or directory.is_symlink():
            raise CaptureError(f"destination is missing or is a symlink: {directory}")
    return vault, inbox, images


def download_images(entries: list[dict], referer: str) -> list[dict]:
    downloaded: list[dict] = []
    total = 0
    for entry in entries[:MAX_IMAGES]:
        index = int(entry["index"])
        try:
            final_url, content_type, data = pinned_https_get(
                str(entry["url"]),
                max_bytes=MAX_IMAGE_BYTES,
                accept="image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1",
                referer=referer,
            )
            if len(data) < 128:
                raise CaptureError("downloaded image is too small")
            total += len(data)
            if total > MAX_TOTAL_IMAGE_BYTES:
                raise CaptureError("total image bytes exceed the task limit")
            mime, extension = sniff_image(data, content_type)
            downloaded.append({
                "index": index,
                "source_url": final_url,
                "mime": mime,
                "extension": extension,
                "bytes": data,
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        except CaptureError as exc:
            downloaded.append({"index": index, "error": str(exc)})
    return downloaded


def build_note(
    article: dict,
    source_url: str,
    image_names: dict[int, str],
    *,
    resolved_source_url: str = "",
) -> bytes:
    body = str(article["markdown"])
    for entry in article["images"]:
        index = int(entry["index"])
        marker = f"{{{{GEOF_IMAGE_{index}}}}}"
        html_marker = f"{{{{GEOF_HTML_IMAGE_{index}}}}}"
        if index in image_names:
            replacement = f"![[06-Attachments/00-images/{image_names[index]}]]"
            html_replacement = (
                '<img src="../06-Attachments/00-images/'
                f'{html.escape(image_names[index], quote=True)}" alt="">'
            )
        else:
            replacement = f"<!-- 图片 {index} 下载失败 -->"
            html_replacement = replacement
        body = body.replace(marker, replacement)
        body = body.replace(html_marker, html_replacement)
    description = normalize_inline_text(str(article.get("description") or ""))
    if description:
        body = f"> 摘要：{description}\n\n{body}"
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    created = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    frontmatter = [
        "---",
        f"title: {yaml_string(str(article['title']))}",
        f"author: {yaml_string(str(article.get('author') or ''))}",
        f"source: {yaml_string(source_url)}",
        *(
            [f"resolved_source: {yaml_string(resolved_source_url)}"]
            if resolved_source_url and resolved_source_url != source_url
            else []
        ),
        f"published: {yaml_string(str(article.get('published') or ''))}",
        *([f"description: {yaml_string(description)}"] if description else []),
        f"created: {yaml_string(created)}",
        "tags:",
        "  - web-clip",
        "---",
        "",
        f"# {article['title']}",
        "",
        body,
        "",
        f"> 来源：[{source_url}]({source_url})",
        f"> 抓取时间：{created}",
        "",
    ]
    return "\n".join(frontmatter).encode("utf-8")


def execute(spec: dict, *, enforce_production: bool = True) -> dict:
    if spec.get("version") != 1:
        raise CaptureError("unsupported task specification version")
    task_id = str(spec.get("task_id") or "").strip()
    if not re.fullmatch(r"task_[A-Za-z0-9_-]{1,100}", task_id):
        raise CaptureError("invalid task id")
    if spec.get("skill_id") != "wechat-to-obsidian":
        raise CaptureError("task specification is bound to another skill")
    source_url = normalize_public_https_url(str(spec.get("source_url") or ""))
    _, inbox, images_dir = validate_destination(spec, enforce_production=enforce_production)

    final_url, content_type, html_bytes = pinned_https_get(
        source_url,
        max_bytes=MAX_HTML_BYTES,
        accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    )
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        raise CaptureError(f"source is not an HTML document: {content_type}")
    article = extract_article(decode_html_document(html_bytes, content_type), final_url)
    downloaded = download_images(article["images"], final_url)

    safe_stem = sanitize_filename(str(article["title"]))
    task_suffix = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8]
    image_names: dict[int, str] = {}
    image_payloads: list[tuple[str, bytes]] = []
    for item in downloaded:
        if item.get("error"):
            continue
        index = int(item["index"])
        filename = f"{safe_stem}-{task_suffix}-{index:02d}.{item['extension']}"
        image_names[index] = filename
        image_payloads.append((filename, item["bytes"]))

    written_images: list[Path] = []
    note_path: Path | None = None
    try:
        for filename, data in image_payloads:
            written_images.append(create_only_bytes(images_dir, filename, data))
        note_bytes = build_note(
            article,
            source_url,
            image_names,
            resolved_source_url=final_url,
        )
        note_path = choose_create_only_name(inbox, safe_stem, ".md", note_bytes)
    except Exception:
        for created_path in reversed(written_images):
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    if note_path is None:
        raise CaptureError("note publishing did not produce a destination path")

    result = {
        "version": 1,
        "ok": True,
        "task_id": task_id,
        "skill_id": "wechat-to-obsidian",
        "note_path": f"00-Inbox/{note_path.name}",
        "note_sha256": hashlib.sha256(note_bytes).hexdigest(),
        "source_url": source_url,
        "final_url": final_url,
        "title": article["title"],
        "content_chars": len(normalize_inline_text(article["markdown"])),
        "math_count": int(article.get("math_count") or 0),
        "article_marker": article.get("article_marker", ""),
        "image_paths": [f"06-Attachments/00-images/{path.name}" for path in written_images],
        "image_count": len(written_images),
        "image_failures": len([item for item in downloaded if item.get("error")]),
        "final_source_host": urlsplit(final_url).hostname,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def write_result(path: Path, result: dict) -> None:
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    spec_path = Path.cwd() / SPEC_NAME
    result_path = Path.cwd() / RESULT_NAME
    try:
        if not spec_path.is_file() or spec_path.is_symlink():
            raise CaptureError("sealed task specification is missing")
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise CaptureError("task specification must be an object")
        enforce_production = os.environ.get("GEOF_COBS_CANARY") != "1"
        result = execute(spec, enforce_production=enforce_production)
        write_result(result_path, result)
        print(json.dumps({
            "ok": True,
            "note_path": result["note_path"],
            "image_count": result["image_count"],
            "image_failures": result["image_failures"],
            "math_count": result["math_count"],
        }, ensure_ascii=False))
        return 0
    except (CaptureError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
