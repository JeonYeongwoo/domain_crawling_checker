"""Menu-driven news-domain discovery, persistent storage, and crawl checks."""
import contextlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

import check

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / "news_store.json"
REPORT_PATH = ROOT / "results.txt"
DOMAINS_PATH = ROOT / "crawlable_domains.txt"
URLS_PATH = ROOT / "crawlable_urls.txt"
FILE_CHECKS_PATH = ROOT / "file_checks"
SEEDS = [
    ("채널 A", "https://ichannela.com/news/main/news_main_renew.do", True),
    ("JTBC 뉴스", "https://news.jtbc.co.kr/", True),
    ("TBC SBS뉴스", "https://www.tbc.co.kr/", True),
    ("YTN 뉴스", "https://www.ytn.co.kr/", True),
    ("KBS 뉴스", "https://news.kbs.co.kr/news/pc/main/main.html", True),
    ("MBC 뉴스", "https://imnews.imbc.com/pc_main.html", False),
]


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def domain_key(url):
    # Keep one URL per hostname, preserving the user's specific news path.
    return urlsplit(check.normalize(url)).hostname.removeprefix("www.")


def add_site(sites, name, url, source):
    url = check.normalize(url)
    key = domain_key(url)
    if any(domain_key(site["url"]) == key for site in sites):
        return False
    sites.append({"name": name.strip() or key, "url": url, "source": source,
                  "added_at": now(), "status": None, "checked_at": None,
                  "result_source": None, "reason": "아직 검사하지 않음", "logs": []})
    return True


def write_atomic(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_sites(sites):
    write_atomic(STORE_PATH, json.dumps({"version": 1, "sites": sites}, ensure_ascii=False, indent=2) + "\n")


def load_sites():
    if not STORE_PATH.exists():
        sites = []
        for name, url, result in SEEDS:
            add_site(sites, name, url, "사용자 제공")
            sites[-1].update(status=result, result_source="사용자 제공", reason="사용자가 제공한 결과 (앱 재검사 전)")
        save_sites(sites)
        return sites
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("sites"), list):
            raise ValueError("지원하지 않는 저장 형식")
        sites = data["sites"]
        for site in sites:
            if not isinstance(site, dict) or not isinstance(site.get("name"), str):
                raise ValueError("언론사 이름 누락")
            check.normalize(site["url"])
            if site.get("status") is not None and type(site["status"]) is not bool:
                raise ValueError("잘못된 결과 값")
        return sites
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"저장 파일을 읽을 수 없습니다: {STORE_PATH}\n기존 파일은 덮어쓰지 않습니다. 원인: {error}") from error


def report(sites, details=False):
    lines = []
    for label, status in (("크롤링 가능", True), ("크롤링 불가능", False), ("미검사", None)):
        group = [(i, s) for i, s in enumerate(sites, 1) if s.get("status") is status]
        lines.append(f"{label} ({len(group)}개)")
        for index, site in group:
            suffix = " [사용자 제공·재검사 전]" if site.get("result_source") == "사용자 제공" else ""
            lines.append(f"{index}. {site['name']} : {site['url']}{suffix}")
            if details and site.get("status") is not None:
                lines.append("   사유: " + site.get("reason", ""))
                if site.get("checked_at"):
                    lines.append("   검사 시각: " + site["checked_at"])
        if not group:
            lines.append("(없음)")
        lines.append("")
    return "\n".join(lines)


def export_report(sites, path=None):
    text = "뉴스 사이트 검사 결과\n저장 시각: " + now() + "\n\n" + report(sites, details=True)
    text += "\n입력 URL의 robots.txt 및 HTTP 텍스트 접근 검사 결과입니다.\n"
    text += "크롤링 불가능에는 규칙 차단뿐 아니라 연결 실패·검사 실패도 포함됩니다.\n"
    text += "홈페이지 검사 성공이 개별 기사 수집 성공이나 수집 허가를 의미하지는 않습니다.\n"
    write_atomic(REPORT_PATH if path is None else path, text)


def export_crawlable(sites):
    allowed = [site for site in sites if site.get("status") is True]
    # Preserve the exact checked URL separately: root access can differ by path.
    urls = list(dict.fromkeys(check.normalize(site["url"]) for site in allowed))
    domains = list(dict.fromkeys(urlsplit(url).hostname for url in urls))
    write_atomic(DOMAINS_PATH, "".join(domain + "\n" for domain in domains))
    write_atomic(URLS_PATH, "".join(url + "\n" for url in urls))
    print(f"\n내보내기 완료: 크롤링 가능 도메인 {len(domains)}개 / URL {len(urls)}개")
    print("도메인 목록: " + str(DOMAINS_PATH))
    print("검사 URL 목록: " + str(URLS_PATH))
    reported = sum(site.get("result_source") == "사용자 제공" for site in allowed)
    if reported:
        print(f"[안내] 사용자 제공·재검사 전 결과 {reported}개가 포함되어 있습니다.")
    print("저장된 최신 판정을 사용합니다. 도메인 전체 경로의 접근 가능 여부를 보장하지 않습니다.")
    if not allowed:
        print("가능으로 분류된 사이트가 없어 빈 목록을 저장했습니다. 크롤링 검사 > 저장된 링크 검사에서 먼저 검사하세요.")


def discover(keyword):
    query = urlencode({"q": keyword, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    url = "https://news.google.com/rss/search?" + query
    check.log("구글 뉴스 RSS 조회: " + url)
    status, headers, body, truncated = check.fetch(url)
    # A feed is a directory: never save its opaque Google article links as publishers.
    if status != 200 or truncated:
        raise ValueError(f"RSS 조회 실패 (HTTP {status}, 응답 크기 초과: {truncated})")
    root = ET.fromstring(body)
    if root.tag not in ("rss", "feed"):
        raise ValueError("뉴스 RSS 형식이 아닙니다.")
    found, seen = [], set()
    for source in root.findall(".//item/source"):
        url = source.get("url", "")
        if not url:
            continue
        try:
            url = check.normalize(url)
            key = domain_key(url)
        except (ValueError, UnicodeError):
            continue
        if key in seen or key == "google.com" or key.endswith(".google.com"):
            continue
        seen.add(key)
        found.append(((source.text or key).strip(), url))
    return found


def import_candidates():
    text = (ROOT / "news_domains.md").read_text(encoding="utf-8")
    return [(match.group(1).strip(), match.group(2)) for match in re.finditer(
        r"^\|\s*(?:국내|해외)\s*\|\s*([^|]+)\|\s*\[[^\]]+\]\((https?://[^)]+)\)", text, re.M)]


class BackToMenu(Exception):
    pass


def ask(prompt):
    value = input(prompt).strip()
    if value.lower() in ("q", "quit", "exit"):
        raise EOFError
    if value.lower() in ("clear", "cls"):
        os.system("cls" if os.name == "nt" else "clear")
        raise BackToMenu
    if value.lower() in ("b", "back"):
        raise BackToMenu
    return value


def explore_menu(sites):
    print("\n1. 구글 뉴스에서 키워드로 추가 탐색\n2. 기존 국내·해외 67개 후보 목록 불러오기\n(b: 메뉴로)")
    choice = ask("탐색 방법 (기본 1): ") or "1"
    if choice == "1":
        keyword = ask("검색어 (예: AI, 경제, 지역뉴스 / 기본: 뉴스): ") or "뉴스"
        found = discover(keyword)
        source = "구글 뉴스 RSS: " + keyword
    elif choice == "2":
        found = import_candidates()
        source = "기존 후보 목록"
    else:
        print("1 또는 2를 입력하세요.")
        return
    added = 0
    for name, url in found:
        if add_site(sites, name, url, source):
            added += 1
            print("[추가] " + name + " : " + url)
    save_sites(sites)
    export_report(sites)
    print(f"\n조회 {len(found)}개 / 신규 저장 {added}개 / 중복 {len(found) - added}개 / 전체 {len(sites)}개")
    print("새 링크는 미검사 상태입니다. 크롤링 검사 > 저장된 링크 검사에서 검사하세요.")
    if choice == "1":
        print("검색어별 RSS에 나온 출처만 조회합니다. 전체 언론사 목록은 아닙니다.")


def manual_menu(sites):
    url = check.normalize(ask("추가할 도메인 또는 URL: "))
    existing = next((s for s in sites if domain_key(s["url"]) == domain_key(url)), None)
    if existing:
        print(f"이미 저장됨: {existing['name']} : {existing['url']}")
        if url == existing["url"]:
            return
        if ask("이 사이트의 검사 URL을 입력한 주소로 바꿀까요? (y/N): ").lower() != "y":
            return
        existing.update(url=url, status=None, checked_at=None, result_source=None,
                        reason="검사 URL 변경: 재검사 필요", logs=[])
    else:
        name = ask("언론사 이름 (생략하면 도메인): ")
        add_site(sites, name, url, "직접 추가")
    save_sites(sites)
    export_report(sites)
    print("저장 완료. 크롤링 검사 > 저장된 링크 검사에서 검사하세요.")


def select_domain_file():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        raise ValueError("파일 선택창을 사용할 수 없습니다. TXT 파일 경로를 직접 입력하세요.") from error
    window = None
    try:
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        return filedialog.askopenfilename(parent=window, title="도메인 목록 TXT 파일 선택",
                                          filetypes=[("도메인 목록", "*.txt")])
    except tk.TclError as error:
        raise ValueError("파일 선택창을 열 수 없습니다. TXT 파일 경로를 직접 입력하세요.") from error
    finally:
        if window is not None:
            window.destroy()


def read_domain_file(path):
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("TXT 파일을 선택하세요. 한 줄에 도메인 또는 URL 하나를 넣으면 됩니다.")
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("파일은 2MB 이하로 업로드하세요.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp949")
        except UnicodeDecodeError as error:
            raise ValueError("UTF-8 또는 CP949로 저장한 TXT 파일을 사용하세요.") from error
    lines = text.splitlines()
    if len(lines) > 10000:
        raise ValueError("파일은 10,000줄 이하로 업로드하세요.")
    found, rejected = [], []
    for number, line in enumerate(lines, 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        name = ""
        match = re.fullmatch(r"(.+?)\s*:\s*(https?://\S+)", value, re.I)
        if match:
            name, value = match.group(1).strip(), match.group(2)
        try:
            url = check.normalize(value)
            host = urlsplit(url).hostname
            if not host or re.search(r"\s", value) or not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
                raise ValueError("올바른 도메인/URL이 아닙니다.")
            if not ("." in host or ":" in host or host == "localhost"):
                raise ValueError("도메인에 점(.)이 없습니다.")
            found.append((name or host, url))
        except (ValueError, UnicodeError) as error:
            rejected.append((number, str(error)))
    return found, rejected


def ask_domain_file():
    value = ask("파일 경로 입력 또는 파일을 이 창에 드래그 (Enter: 파일 선택창): ")
    value = value.strip('"\'') if value else select_domain_file()
    if not value:
        print("파일 선택을 취소했습니다.")
        return None
    return Path(value).expanduser().resolve()


def print_rejected(rejected):
    for number, reason in rejected[:20]:
        print(f"[제외] {number}번째 줄: {reason}")
    if len(rejected) > 20:
        print(f"[제외] 나머지 오류 {len(rejected) - 20}개는 표시를 생략합니다.")


def upload_menu(sites):
    print("\nTXT 파일: 한 줄에 도메인/URL 하나 또는 '언론사 이름 : https://주소'")
    path = ask_domain_file()
    if path is None:
        return
    found, rejected = read_domain_file(path)
    added = 0
    for name, url in found:
        if add_site(sites, name, url, "파일 업로드: " + path.name):
            added += 1
            print("[추가] " + name + " : " + url)
    if added:
        save_sites(sites)
        export_report(sites)
    print_rejected(rejected)
    print(f"\n파일 불러오기 완료: 신규 {added}개 / 중복 {len(found) - added}개 / 잘못된 줄 {len(rejected)}개")
    if added:
        print("앱에 저장했습니다. 크롤링 검사 > 저장된 링크 검사에서 new를 입력하면 미검사 링크를 검사할 수 있습니다.")
    elif not found:
        print("추가할 도메인이 없습니다. 파일 내용을 확인하세요.")


class Tee:
    def __init__(self, stream, buffer):
        self.stream, self.buffer = stream, buffer

    def write(self, text):
        self.stream.write(text)
        self.buffer.append(text)
        return len(text)

    def flush(self):
        self.stream.flush()


def inspect_site(site):
    captured = []
    with contextlib.redirect_stdout(Tee(sys.stdout, captured)):
        result = check.check(site["url"])
    logs = "".join(captured).splitlines()
    site.update(status=result, checked_at=now(), result_source="앱 검사", logs=logs,
                reason=logs[-1].removeprefix("[로그] ") if logs else "검사 로그 없음")
    return result


def select_sites(sites, value):
    if value in ("", "all", "전체"):
        return sites[:]
    if value.lower() in ("new", "미검사"):
        return [s for s in sites if s.get("status") is None or s.get("result_source") == "사용자 제공"]
    try:
        indices = list(dict.fromkeys(int(v.strip()) for v in value.split(",")))
        if any(i < 1 or i > len(sites) for i in indices):
            raise ValueError
    except ValueError:
        raise ValueError("전체, new 또는 목록의 번호를 입력하세요. 예: 1,3,6") from None
    return [sites[i - 1] for i in indices]


def inspect_menu(sites):
    print("\n" + report(sites))
    targets = select_sites(sites, ask("검사 대상 (Enter: 전체 / new: 미검사·사용자 제공 / 번호: 1,3,6): "))
    if not targets:
        print("검사할 사이트가 없습니다.")
        return
    print(f"\n{len(targets)}개 검사 시작. Ctrl+C로 중단하면 완료된 결과는 보존됩니다.")
    try:
        for index, site in enumerate(targets, 1):
            print(f"\n[{index}/{len(targets)}] {site['name']} : {site['url']}", flush=True)
            result = inspect_site(site)
            save_sites(sites)
            export_report(sites)
            print("크롤링 가능" if result else "크롤링 불가능", flush=True)
    except KeyboardInterrupt:
        print("\n검사를 중단했습니다. 완료된 결과는 저장되어 있습니다.")
    print("\n" + report(sites))
    print("결과 저장: " + str(REPORT_PATH))


def delete_menu(sites):
    if not sites:
        print("저장된 도메인이 없습니다.")
        return
    print("\n" + report(sites))
    value = ask("삭제할 번호 (예: 1,3,6 / all: 전체 삭제 / Enter: 취소): ")
    if not value:
        print("삭제를 취소했습니다.")
        return
    if value.lower() not in ("all", "전체") and not re.fullmatch(r"\d+(\s*,\s*\d+)*", value):
        raise ValueError("삭제할 번호 또는 all을 입력하세요. 예: 1,3,6")
    targets = select_sites(sites, value.lower())
    removed_ids = {id(site) for site in targets}
    remaining = [site for site in sites if id(site) not in removed_ids]
    # Persist before mutating the in-memory list so a failed write preserves it.
    save_sites(remaining)
    sites[:] = remaining
    export_report(sites)
    if DOMAINS_PATH.exists() or URLS_PATH.exists():
        export_crawlable(sites)
    for site in targets:
        print("[삭제] " + site["name"] + " : " + site["url"])
    print(f"삭제 완료: {len(targets)}개 / 남은 도메인 {len(sites)}개")


def save_file_check(directory, source, targets, rejected):
    export_report(targets, directory / "results.txt")
    with (directory / "results.txt").open("a", encoding="utf-8") as stream:
        stream.write("\n입력 파일: " + str(source) + "\n")
        for number, reason in rejected:
            stream.write(f"제외된 줄 {number}: {reason}\n")
    write_atomic(directory / "results.json", json.dumps({
        "source_file": str(source), "saved_at": now(), "sites": targets,
        "rejected_lines": [{"line": number, "reason": reason} for number, reason in rejected],
    }, ensure_ascii=False, indent=2) + "\n")
    for filename, status in (("possible_urls.txt", True), ("impossible_urls.txt", False)):
        write_atomic(directory / filename, "".join(site["url"] + "\n" for site in targets if site["status"] is status))


def inspect_file_menu():
    print("\n선택한 TXT 파일 안의 링크만 검사합니다. 저장된 도메인 목록은 변경하지 않습니다.")
    path = ask_domain_file()
    if path is None:
        return
    found, rejected = read_domain_file(path)
    print_rejected(rejected)
    targets, seen = [], set()
    for name, url in found:
        # Different paths on the same host can have different robots rules.
        if url in seen:
            continue
        seen.add(url)
        record = []
        add_site(record, name, url, "파일 검사: " + path.name)
        targets.append(record[0])
    print(f"정상 URL {len(targets)}개 / 중복 {len(found) - len(targets)}개 / 잘못된 줄 {len(rejected)}개")
    if not targets:
        print("검사할 URL이 없습니다.")
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = re.sub(r"[^\w.-]", "_", path.stem)[:60]
    directory = FILE_CHECKS_PATH / (stamp + "_" + filename)
    directory.mkdir(parents=True, exist_ok=False)
    save_file_check(directory, path, targets, rejected)
    print("검사 시작. Ctrl+C로 중단하면 완료된 결과는 보존됩니다.")
    try:
        for index, site in enumerate(targets, 1):
            print(f"\n[{index}/{len(targets)}] {site['name']} : {site['url']}", flush=True)
            result = inspect_site(site)
            save_file_check(directory, path, targets, rejected)
            print("크롤링 가능" if result else "크롤링 불가능", flush=True)
    except KeyboardInterrupt:
        print("\n파일 검사를 중단했습니다. 완료되지 않은 링크는 미검사로 표시합니다.")
    finally:
        save_file_check(directory, path, targets, rejected)
    print("\n" + report(targets, details=True))
    print("파일 검사 결과 폴더: " + str(directory))
    print("results.txt: 분류와 사유 / results.json: 상세 로그")
    print("possible_urls.txt: 가능 / impossible_urls.txt: 불가능")


def logs_menu(sites):
    print("\n" + report(sites))
    value = ask("로그를 볼 사이트 번호: ")
    if not value.isdigit() or not 1 <= int(value) <= len(sites):
        print("목록에 있는 번호를 입력하세요.")
        return
    site = sites[int(value) - 1]
    print(site["name"] + " : " + site["url"])
    print("\n".join(site.get("logs", [])) or site.get("reason", "검사 로그 없음"))


def show_saved_results(sites):
    print("\n" + report(sites, details=True))
    export_report(sites)
    print("결과 저장: " + str(REPORT_PATH))


def clear_screen():
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def print_menu(title, labels, count, main=False):
    print("\n" + "=" * 56)
    print(f"  {title}  |  저장된 사이트: {count}개")
    print("=" * 56 + "\n")
    for number, label in enumerate(labels, 1):
        print(f"    {number}. {label}\n")
    print("-" * 56)
    print("  clear / cls : 화면 지우기")
    print("  q / 0 : 종료" if main else "  b / 0 : 메인 메뉴로    q : 종료")
    print("=" * 56 + "\n")


def submenu(sites, category):
    groups = {
        "1": ("도메인 등록 및 관리", [
            ("뉴스 도메인 추가 탐색 및 저장", lambda: explore_menu(sites)),
            ("도메인 직접 추가 / 검사 URL 변경", lambda: manual_menu(sites)),
            ("도메인 목록 파일 업로드 (TXT)", lambda: upload_menu(sites)),
            ("저장된 도메인 삭제", lambda: delete_menu(sites)),
        ]),
        "2": ("크롤링 검사", [
            ("저장된 링크 크롤링 가능 여부 검사", lambda: inspect_menu(sites)),
            ("지정한 TXT 파일만 바로 검사", inspect_file_menu),
        ]),
        "3": ("결과 확인 및 내보내기", [
            ("가능 / 불가능 / 미검사 결과 보기", lambda: show_saved_results(sites)),
            ("사이트별 검사 로그 보기", lambda: logs_menu(sites)),
            ("크롤링 가능한 도메인 내보내기 (TXT)", lambda: export_crawlable(sites)),
        ]),
    }
    title, actions = groups[category]
    clear_screen()
    while True:
        print_menu(title, [label for label, _ in actions], len(sites))
        try:
            choice = ask("세부 기능 선택: ")
        except BackToMenu:
            return
        if choice == "0":
            return
        if not choice:
            continue
        if choice not in [str(i) for i in range(1, len(actions) + 1)]:
            print(f"1~{len(actions)} 중 선택하세요.")
            continue
        try:
            clear_screen()
            actions[int(choice) - 1][1]()
        except BackToMenu:
            continue
        except KeyboardInterrupt:
            print("\n세부 메뉴로 돌아갑니다.")
        except (ValueError, OSError, ET.ParseError) as error:
            print("[오류] " + str(error))


def run():
    try:
        sites = load_sites()
        export_report(sites)
    except (OSError, ValueError) as error:
        print("[오류] " + str(error))
        return 1
    clear_screen()
    print("뉴스 도메인 탐색 및 크롤링 검사")
    while True:
        print_menu("메인 메뉴", ["도메인 등록 및 관리", "크롤링 검사", "결과 확인 및 내보내기"], len(sites), main=True)
        try:
            choice = ask("기능 선택: ")
            if choice == "0":
                return 0
            if choice in ("1", "2", "3"):
                submenu(sites, choice)
                clear_screen()
            elif choice:
                print("1~3 중 선택하세요.")
        except BackToMenu:
            continue
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print("\n메뉴로 돌아갑니다.")
        except (ValueError, OSError, ET.ParseError) as error:
            print("[오류] " + str(error))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(run())
