"""Dependency-free, conservative HTTP/robots.txt crawlability checker."""
import os
import re
import sys
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENT = "CrawlCheck"
TIMEOUT = 10
LIMIT = 1024 * 1024


def log(message):
    print("[로그] " + message, flush=True)


def normalize(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("http/https 도메인 또는 URL을 입력하세요.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("아이디/비밀번호가 포함된 URL은 지원하지 않습니다.")
    host = parts.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    if parts.port:
        host += ":" + str(parts.port)
    return urlunsplit((parts.scheme, host, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="%=&?/:@!$'()*+,;~-._"), ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url):
    request = Request(url, headers={"User-Agent": AGENT + "/1.0 (crawlability check)",
                                    "Accept-Encoding": "identity"})
    try:
        response = build_opener(NoRedirect).open(request, timeout=TIMEOUT)
    except HTTPError as error:
        response = error
    with response:
        data = response.read(LIMIT + 1)
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            body = data[:LIMIT].decode(charset, errors="replace")
        except LookupError:
            body = data[:LIMIT].decode("utf-8", errors="replace")
        return response.status, response.headers, body, len(data) > LIMIT


def canonical_path(value):
    value = quote(value, safe="/%?=&:@!$'()*+,;~-._")
    def replace(match):
        char = chr(int(match.group(1), 16))
        return char if re.fullmatch(r"[A-Za-z0-9._~-]", char) else match.group(0).upper()
    return re.sub(r"%([0-9a-fA-F]{2})", replace, value)


def robots_allowed(body, url):
    groups, agents, rules = [], [], []
    for line in body.lstrip("\ufeff").splitlines():
        key, sep, value = line.split("#", 1)[0].partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((key, value))
    groups.append((agents, rules))
    specific = [r for a, r in groups if AGENT.lower() in a]
    selected = specific if specific else [r for a, r in groups if "*" in a]
    parts = urlsplit(url)
    path = canonical_path((parts.path or "/") + ("?" + parts.query if parts.query else ""))
    matches = []
    for group in selected:
        for key, pattern in group:
            if not pattern:
                continue
            pattern = canonical_path(pattern)
            end = pattern.endswith("$")
            raw = pattern[:-1] if end else pattern
            expression = "^" + ".*".join(re.escape(p) for p in raw.split("*")) + ("$" if end else "")
            if re.search(expression, path):
                matches.append((len(pattern.encode("utf-8")), key == "allow", key + ": " + pattern))
    if not matches:
        return True, "해당 경로에 적용되는 차단 규칙 없음"
    _, allowed, rule = max(matches)
    return allowed, rule


def check_robots(url):
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    log("robots.txt 확인: " + robots_url)
    for _ in range(6):
        status, headers, body, truncated = fetch(robots_url)
        log("robots.txt HTTP " + str(status))
        if status in (301, 302, 303, 307, 308) and headers.get("Location"):
            robots_url = normalize(urljoin(robots_url, headers["Location"]))
            continue
        if status in (404, 410):
            log("robots.txt 없음: 공개 차단 규칙을 확인할 수 없음")
            return True
        if not 200 <= status < 300:
            log("robots.txt 조회 실패: 보수적으로 불가능 처리")
            return False
        if truncated or re.search(r"<(?:!doctype\s+html|html)\b", body, re.I):
            log("robots.txt 응답이 너무 크거나 HTML이므로 규칙 확인 실패")
            return False
        allowed, reason = robots_allowed(body, url)
        log("robots.txt 판단: " + reason)
        return allowed
    log("robots.txt 리디렉션 횟수 초과")
    return False


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def check(value):
    try:
        url = normalize(value)
        log("검사 대상: " + url)
        log("검사 User-Agent: " + AGENT)
        for _ in range(6):
            if not check_robots(url):
                return False
            status, headers, body, truncated = fetch(url)
            log("페이지 HTTP " + str(status))
            if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                url = normalize(urljoin(url, headers["Location"]))
                log("이동 대상: " + url)
                continue
            if not 200 <= status < 300:
                log("접근 실패/차단 또는 서버 오류")
                return False
            content_type = headers.get("Content-Type", "").lower()
            log("응답 형식: " + (content_type or "알 수 없음"))
            if not any(t in content_type for t in ("text/", "json", "xml", "xhtml")):
                log("텍스트 기반 페이지가 아니므로 이 검사에서는 불가능 처리")
                return False
            parser = VisibleText()
            parser.feed(body)
            visible = " ".join(" ".join(parser.text).split())
            if any(phrase in visible.lower() for phrase in (
                "verify you are human", "checking your browser", "just a moment...",
                "enable javascript and cookies to continue", "사람인지 확인")):
                log("봇 확인/브라우저 검증 화면 감지")
                return False
            log("읽을 수 있는 텍스트: " + str(len(visible)) + "자")
            if len(visible) < 80:
                log("본문 텍스트 부족: 빈 페이지 또는 JavaScript 렌더링 필요 가능성")
                return False
            if truncated:
                log("최대 1MB까지만 읽어 검사함")
            log("규칙상 접근 가능하며 HTTP로 텍스트 수신 성공")
            return True
        log("페이지 리디렉션 횟수 초과")
    except (ValueError, URLError, OSError, HTTPError) as error:
        log("검사 실패: " + str(error))
    return False


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if len(sys.argv) > 1:
        result = check(sys.argv[1])
        print("가능하다" if result else "불가능하다")
        return 0 if result else 1
    from app import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
