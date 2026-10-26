"""Shared engine for the GCE Paper Scrapper scripts.

Each `GCE Paper Scrapper (<Subject>).py` file is a thin launcher around this
module; all scraping, filtering and download logic lives here so a fix lands
once for every subject.
"""

import argparse
import hashlib
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SITE_HOST = "cameroongcerevision.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

DEFAULT_WORKERS = 10
PAGE_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 30
MAX_PDF_BYTES = 200 * 1024 * 1024  # refuse anything larger than 200 MB
PDF_SNIFF_BYTES = 1024  # the PDF spec allows the header anywhere in the first KB
MAX_FILENAME_STEM = 120
UNKNOWN_YEAR = "Unknown_Year"

GOOGLE_HOSTS = {"docs.google.com", "drive.google.com"}


# ===================================
# SUBJECT CONFIG
# ===================================

@dataclass(frozen=True)
class Subject:
    """One scrapeable subject.

    `require` and `ban` are matched against whole words of the page's URL path
    (`ict` will not match `predict`). Multi-word entries use hyphens, e.g.
    `computer-science`. A page is kept when it matches any `require` word and
    none of the `ban` words.
    """

    name: str
    start_url: str
    save_folder: str
    require: tuple
    ban: tuple = ()


_OTHER_SUBJECTS = (
    "chemistry", "biology", "physics", "economics", "geography", "commerce",
    "entrepreneurship", "entrepreneur", "religious", "history", "o-level",
)


def _ban(*extra, without=()):
    return tuple(w for w in _OTHER_SUBJECTS if w not in without) + extra


SUBJECTS = {
    "math": Subject(
        "Mathematics",
        "https://cameroongcerevision.com/a-level/cameroon-gce-questions-mathematics-a-level/",
        "GCE-MATH",
        require=("math", "maths", "mathematics"),
        ban=_ban("computer-science", "ict", "f-math", "further"),
    ),
    "further-math": Subject(
        "Further Mathematics",
        "https://cameroongcerevision.com/a-level/june-f-math-a-level/",
        "GCE-F-MATH",
        require=("f-math", "further-math", "further"),
        ban=_ban("computer-science", "ict"),
    ),
    "physics": Subject(
        "Physics",
        "https://cameroongcerevision.com/a-level/cameroon-gce-questions-a-level-physics/",
        "GCE-PHYSICS",
        require=("physics",),
        ban=_ban("computer-science", "ict", "math", "maths", "mathematics",
                 without=("physics",)),
    ),
    "chemistry": Subject(
        "Chemistry",
        "https://cameroongcerevision.com/a-level/june-chemistry-a-level/",
        "GCE-CHEMISTRY",
        require=("chemistry",),
        ban=_ban("computer-science", "ict", "math", "maths", "mathematics",
                 without=("chemistry",)),
    ),
    "biology": Subject(
        "Biology",
        "https://cameroongcerevision.com/a-level/june-biology-a-level/",
        "GCE-BIOLOGY",
        require=("biology",),
        ban=_ban("computer-science", "ict", "math", "maths", "mathematics",
                 without=("biology",)),
    ),
    "computer-science": Subject(
        "Computer Science",
        "https://cameroongcerevision.com/a-level/june-computer-science/",
        "GCE-CSC",
        require=("computer-science", "computer"),
        ban=_ban("ict", "math", "maths", "mathematics"),
    ),
    "ict": Subject(
        "ICT",
        "https://cameroongcerevision.com/a-level/cameroon-gce-questions-a-level-ict/",
        "GCE-ICT",
        require=("ict",),
        ban=_ban("computer-science", "math", "maths", "mathematics"),
    ),
}


# ===================================
# PURE HELPERS (no network, unit-tested)
# ===================================

def fix_url(url):
    """Resolve protocol-relative URLs (`//host/x`) to https."""
    return "https:" + url if url.startswith("//") else url


def is_http_url(url):
    return urlparse(url).scheme in ("http", "https")


def is_site_url(url):
    """True for http(s) URLs on the scraped site (or one of its subdomains)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        host == SITE_HOST or host.endswith("." + SITE_HOST)
    )


def _slug(url):
    """Hyphen-delimited, lowercase path words: `/a/B_c/` -> `-a-b-c-`."""
    words = re.split(r"[^a-z0-9]+", urlparse(url).path.lower())
    return "-" + "-".join(w for w in words if w) + "-"


def is_subject_page(url, subject):
    """Should `url` be scanned for this subject's papers?"""
    if not is_site_url(url):
        return False
    slug = _slug(url)
    if any(f"-{word}-" in slug for word in subject.ban):
        return False
    return any(f"-{word}-" in slug for word in subject.require)


def convert_google_drive(url):
    """Turn a Google Docs/Drive file link into a direct-download link."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() in GOOGLE_HOSTS:
        match = re.search(r"/d/([\w-]+)", parsed.path)
        if match:
            return f"https://docs.google.com/uc?export=download&id={match.group(1)}"
    return url


def extract_year(text):
    match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text)
    return match.group(1) if match else UNKNOWN_YEAR


def safe_filename(pdf_url):
    """A filesystem-safe `.pdf` filename derived from a download URL.

    Names come from remote data, so path separators, traversal segments,
    control characters and hidden-file dots are all neutralised.
    """
    parsed = urlparse(pdf_url)
    if (parsed.hostname or "").lower() in GOOGLE_HOSTS:
        file_id = re.sub(r"[^\w-]", "", parse_qs(parsed.query).get("id", [""])[0])
        if file_id:
            return f"gdrive-{file_id}.pdf"

    name = unquote(parsed.path.rsplit("/", 1)[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^\w.\-() ]+", "_", name).strip(" ._")
    name = name[:MAX_FILENAME_STEM] or "paper"
    return name + ".pdf"


def plan_downloads(tasks, save_folder):
    """Map `(pdf_url, source_page)` tasks to unique `(pdf_url, filepath)` pairs.

    Deterministic regardless of thread timing: tasks are processed in sorted
    order, duplicate URLs collapse, and two different URLs that want the same
    path get a short URL hash appended to the later one.
    """
    plan, taken, seen = [], set(), set()
    for raw_url, source_page in sorted(set(tasks)):
        pdf_url = convert_google_drive(fix_url(raw_url))
        if pdf_url in seen or not is_http_url(pdf_url):
            continue
        seen.add(pdf_url)

        folder = os.path.join(save_folder, extract_year(source_page))
        filename = safe_filename(pdf_url)
        path = os.path.join(folder, filename)
        if path in taken:
            digest = hashlib.sha1(pdf_url.encode()).hexdigest()[:8]
            stem = filename[:-len(".pdf")]
            path = os.path.join(folder, f"{stem}-{digest}.pdf")
        taken.add(path)
        plan.append((pdf_url, path))
    return plan


def find_pdf_urls(html, page_url):
    """PDF URLs on a page: Google-viewer iframes first, then direct links."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    for iframe in soup.find_all("iframe"):
        src = fix_url(iframe.get("src", ""))
        parsed = urlparse(src)
        if (parsed.hostname or "").lower() == "docs.google.com" and parsed.path.startswith("/gview"):
            target = parse_qs(parsed.query).get("url", [None])[0]
            if target:
                found.append(target)
        elif (parsed.hostname or "").lower() in GOOGLE_HOSTS and "/d/" in parsed.path:
            found.append(src)

    for a in soup.find_all("a", href=True):
        if ".pdf" in a["href"].lower():
            found.append(urljoin(page_url, a["href"]))

    return found


def find_subject_pages(html, start_url, subject):
    """Subject page links inside the listing page's <article> blocks, deduped."""
    soup = BeautifulSoup(html, "html.parser")
    pages = []
    for article in soup.find_all("article"):
        for a in article.find_all("a", href=True):
            link = urljoin(start_url, a["href"]).split("#")[0]
            if link not in pages and is_subject_page(link, subject):
                pages.append(link)
    return pages


# ===================================
# NETWORK
# ===================================

def make_session(workers=DEFAULT_WORKERS):
    """Pooled session that retries transient failures (429 / 5xx) with backoff."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(pool_connections=workers, pool_maxsize=workers, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class Scraper:
    def __init__(self, subject, out_dir=".", workers=DEFAULT_WORKERS, session=None):
        self.subject = subject
        self.save_folder = os.path.join(out_dir, subject.save_folder)
        self.workers = workers
        self.session = session or make_session(workers)
        self._lock = threading.Lock()
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    def _count(self, key):
        with self._lock:
            self.stats[key] += 1

    # --- discovery ---

    def list_pages(self):
        res = self.session.get(self.subject.start_url, timeout=PAGE_TIMEOUT)
        res.raise_for_status()
        return find_subject_pages(res.text, self.subject.start_url, self.subject)

    def scan_page(self, page_url):
        print("🔎 Scanning:", page_url)
        try:
            res = self.session.get(page_url, timeout=PAGE_TIMEOUT)
            res.raise_for_status()
        except requests.RequestException as e:
            print(f"❌ Could not read {page_url}: {e}")
            self._count("failed")
            return []
        return [(url, page_url) for url in find_pdf_urls(res.text, page_url)]

    # --- download ---

    def download(self, pdf_url, filepath):
        if os.path.exists(filepath):
            self._count("skipped")
            return

        part = filepath + ".part"
        try:
            with self.session.get(pdf_url, stream=True, allow_redirects=True,
                                  timeout=DOWNLOAD_TIMEOUT) as r:
                r.raise_for_status()
                chunks = r.iter_content(8192)
                first = next(chunks, b"")
                if b"%PDF" not in first[:PDF_SNIFF_BYTES]:
                    print("⚠ Skipped (not a PDF):", pdf_url)
                    self._count("skipped")
                    return

                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                print("⬇ Downloading:", os.path.relpath(filepath, self.save_folder))
                size = len(first)
                with open(part, "wb") as f:
                    f.write(first)
                    for chunk in chunks:
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise ValueError("file exceeds size limit")
                        f.write(chunk)
            os.replace(part, filepath)
            self._count("downloaded")
        except (requests.RequestException, OSError, ValueError) as e:
            print(f"❌ Failed {pdf_url}: {e}")
            self._count("failed")
            try:
                os.remove(part)
            except OSError:
                pass

    # --- orchestration ---

    def run(self):
        os.makedirs(self.save_folder, exist_ok=True)
        pages = self.list_pages()
        print(f"Found {len(pages)} {self.subject.name} page(s)")

        tasks = []
        with ThreadPoolExecutor(self.workers) as pool:
            for future in as_completed([pool.submit(self.scan_page, p) for p in pages]):
                tasks.extend(future.result())

        plan = plan_downloads(tasks, self.save_folder)
        with ThreadPoolExecutor(self.workers) as pool:
            for future in as_completed([pool.submit(self.download, u, p) for u, p in plan]):
                future.result()

        s = self.stats
        print(f"\n✅ DONE — {s['downloaded']} downloaded, "
              f"{s['skipped']} skipped, {s['failed']} failed")
        return s


# ===================================
# CLI
# ===================================

def main(subject_key=None, argv=None):
    parser = argparse.ArgumentParser(description="Download GCE A-Level past papers.")
    if subject_key is None:
        parser.add_argument("subject", choices=sorted(SUBJECTS))
    parser.add_argument("--out", default=".", help="parent folder for the download folder")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)

    subject = SUBJECTS[subject_key or args.subject]
    try:
        Scraper(subject, args.out, max(1, args.workers)).run()
    except requests.RequestException as e:
        sys.exit(f"❌ Could not reach {subject.start_url}: {e}")


if __name__ == "__main__":
    main()
