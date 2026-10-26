import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gce_scraper as g  # noqa: E402

BASE = "https://cameroongcerevision.com/a-level/"


# ---------- subject filters ----------

@pytest.mark.parametrize("key, slug", [
    ("physics", "june-2023-physics-a-level"),
    ("chemistry", "june-2022-chemistry-a-level-paper-1"),
    ("biology", "june-2021-biology-a-level"),
    ("computer-science", "june-2023-computer-science-a-level"),
    ("ict", "june-2023-ict-a-level"),
    ("math", "june-2023-mathematics-a-level"),
    ("further-math", "june-2023-f-math-a-level"),
])
def test_each_subject_accepts_its_own_pages(key, slug):
    # Regression: capitalised keywords were compared with a lowercased URL,
    # so Biology/Chemistry/CS/Physics rejected every page; ICT filtered on "math".
    assert g.is_subject_page(BASE + slug + "/", g.SUBJECTS[key])


@pytest.mark.parametrize("key, slug", [
    ("physics", "june-2023-chemistry-a-level"),
    ("physics", "june-2023-mathematics-a-level"),
    ("ict", "june-2023-mathematics-a-level"),
    ("ict", "june-2023-predict-results"),           # whole-word match only
    ("math", "june-2023-f-math-a-level"),           # further maths is its own subject
    ("math", "june-2023-physics-a-level"),
    ("computer-science", "june-2023-ict-a-level"),
    ("biology", "june-2023-o-level-biology"),
])
def test_subjects_reject_other_subjects(key, slug):
    assert not g.is_subject_page(BASE + slug + "/", g.SUBJECTS[key])


def test_rejects_off_site_and_non_http_links():
    s = g.SUBJECTS["physics"]
    assert not g.is_subject_page("https://evil.example/physics-paper/", s)
    assert not g.is_subject_page("https://cameroongcerevision.com.evil.example/physics/", s)
    assert not g.is_subject_page("javascript:physics()", s)
    assert not g.is_subject_page("ftp://cameroongcerevision.com/physics/", s)


def test_domain_is_not_matched_as_subject_text():
    # "ict" must not match via the host or substrings of other words
    assert not g.is_subject_page("https://cameroongcerevision.com/a-level/district-notes/", g.SUBJECTS["ict"])


# ---------- helpers ----------

def test_extract_year():
    assert g.extract_year(BASE + "june-2023-physics/") == "2023"
    assert g.extract_year(BASE + "physics-12023/") == g.UNKNOWN_YEAR
    assert g.extract_year(BASE + "physics/") == g.UNKNOWN_YEAR


def test_convert_google_drive():
    want = "https://docs.google.com/uc?export=download&id=ABC_123-x"
    assert g.convert_google_drive("https://docs.google.com/file/d/ABC_123-x/preview") == want
    assert g.convert_google_drive("https://drive.google.com/file/d/ABC_123-x/view?usp=sharing") == want
    # host must match exactly - substring tricks are not Google links
    spoof = "https://evil.example/?x=docs.google.com/d/ABC"
    assert g.convert_google_drive(spoof) == spoof


@pytest.mark.parametrize("url, expected", [
    ("https://x.org/files/Paper%201.pdf?dl=1", "Paper 1.pdf"),
    ("https://x.org/files/paper", "paper.pdf"),
    ("https://x.org/files/PAPER.PDF", "PAPER.pdf"),
    ("https://x.org/files/", "paper.pdf"),
    ("https://docs.google.com/uc?export=download&id=AbC-1_2", "gdrive-AbC-1_2.pdf"),
])
def test_safe_filename(url, expected):
    assert g.safe_filename(url) == expected


@pytest.mark.parametrize("url", [
    "https://x.org/a/..%2F..%2Fetc%2Fpasswd.pdf",
    "https://x.org/a/%2e%2e%5c%2e%2e%5cwin.pdf",
    "https://x.org/a/.hidden.pdf",
    "https://x.org/a/bad%00name%0a.pdf",
    "https://x.org/a/" + "A" * 500 + ".pdf",
])
def test_safe_filename_cannot_escape_or_hide(url):
    name = g.safe_filename(url)
    assert os.path.basename(name) == name
    assert "/" not in name and "\\" not in name and "\x00" not in name
    assert not name.startswith(".") and ".." not in name
    assert name.endswith(".pdf") and len(name) <= g.MAX_FILENAME_STEM + 4


def test_plan_downloads_is_deterministic_and_collision_safe(tmp_path):
    src = BASE + "june-2023-physics/"
    tasks = [
        ("https://a.example/one/paper.pdf", src),
        ("https://b.example/two/paper.pdf", src),   # same filename, different file
        ("https://a.example/one/paper.pdf", src),   # duplicate
        ("//c.example/x/other.pdf", BASE + "no-year/"),
    ]
    plan = g.plan_downloads(tasks, str(tmp_path))
    assert plan == g.plan_downloads(list(reversed(tasks)), str(tmp_path))
    paths = [p for _, p in plan]
    assert len(paths) == len(set(paths)) == 3
    assert str(tmp_path / "2023" / "paper.pdf") in paths
    assert str(tmp_path / g.UNKNOWN_YEAR / "other.pdf") in paths
    assert all(os.path.commonpath([str(tmp_path), p]) == str(tmp_path) for p in paths)


def test_plan_downloads_drops_non_http_urls(tmp_path):
    assert g.plan_downloads([("file:///etc/passwd", BASE), ("ftp://h/x.pdf", BASE)], str(tmp_path)) == []


# ---------- HTML parsing ----------

PAGE_HTML = """
<html><body>
<iframe src="//docs.google.com/gview?url=https%3A%2F%2Fcdn.example%2Fp1.pdf&embedded=true"></iframe>
<iframe src="https://evil.example/gview?url=https%3A%2F%2Fevil.example%2Fx.pdf"></iframe>
<iframe src="https://drive.google.com/file/d/FILEID/preview"></iframe>
<a href="/files/p2.PDF">p2</a>
<a href="notes.html">notes</a>
</body></html>
"""


def test_find_pdf_urls():
    assert g.find_pdf_urls(PAGE_HTML, BASE + "june-2023-physics/") == [
        "https://cdn.example/p1.pdf",
        "https://drive.google.com/file/d/FILEID/preview",
        "https://cameroongcerevision.com/files/p2.PDF",
    ]


LISTING_HTML = """
<nav><a href="/a-level/june-2023-physics/">outside article: ignored</a></nav>
<article>
  <a href="/a-level/june-2023-physics/">2023</a>
  <a href="/a-level/june-2023-physics/#comments">2023 again</a>
  <a href="june-2022-physics-paper-2/">2022</a>
  <a href="/a-level/june-2022-chemistry/">wrong subject</a>
  <a href="https://other.example/physics/">off site</a>
</article>
"""


def test_find_subject_pages_dedupes_and_filters():
    pages = g.find_subject_pages(LISTING_HTML, BASE + "physics-listing", g.SUBJECTS["physics"])
    assert pages == [
        "https://cameroongcerevision.com/a-level/june-2023-physics/",
        "https://cameroongcerevision.com/a-level/june-2022-physics-paper-2/",
    ]


# ---------- end to end with a fake network ----------

class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.content, self.status = body, status
        self.text = body.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.requested = routes, []

    def get(self, url, **kw):
        self.requested.append(url)
        if url not in self.routes:
            return FakeResponse(status=404)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


PDF = b"%PDF-1.7\n" + b"x" * 20000


def build_site():
    phys = g.SUBJECTS["physics"]
    page1, page2 = BASE + "june-2023-physics/", BASE + "june-2022-physics/"
    return {
        phys.start_url: (
            f'<article><a href="{page1}">a</a><a href="{page2}">b</a>'
            f'<a href="{BASE}june-2023-chemistry/">c</a></article>'
        ).encode(),
        page1: (
            b'<iframe src="https://docs.google.com/gview?url=https%3A%2F%2Fcdn.example%2Fp1.pdf"></iframe>'
            b'<iframe src="https://drive.google.com/file/d/IDONE/preview"></iframe>'
            b'<iframe src="https://drive.google.com/file/d/IDTWO/preview"></iframe>'
        ),
        page2: b'<a href="https://cdn.example/old/p1.pdf">dup name</a><a href="https://cdn.example/html.pdf">x</a>',
        "https://cdn.example/p1.pdf": PDF,
        "https://cdn.example/old/p1.pdf": PDF + b"2",
        "https://cdn.example/html.pdf": b"<html>login wall</html>",
        "https://docs.google.com/uc?export=download&id=IDONE": PDF + b"A",
        "https://docs.google.com/uc?export=download&id=IDTWO": requests.ConnectionError("boom"),
    }


def test_full_run(tmp_path, capsys):
    session = FakeSession(build_site())
    stats = g.Scraper(g.SUBJECTS["physics"], str(tmp_path), workers=4, session=session).run()

    root = tmp_path / "GCE-PHYSICS"
    assert (root / "2023" / "p1.pdf").read_bytes() == PDF
    assert (root / "2023" / "gdrive-IDONE.pdf").read_bytes() == PDF + b"A"   # not "uc.pdf"
    assert (root / "2022" / "p1.pdf").read_bytes() == PDF + b"2"
    assert not (root / "2022" / "html.pdf").exists()                         # not a PDF -> rejected
    assert not list(root.rglob("*.part"))                                    # no partial files left
    assert stats == {"downloaded": 3, "skipped": 1, "failed": 1}
    assert not any("chemistry" in u for u in session.requested)              # other subject never fetched


def test_second_run_skips_existing_files(tmp_path):
    site = build_site()
    g.Scraper(g.SUBJECTS["physics"], str(tmp_path), session=FakeSession(site)).run()
    session = FakeSession(site)
    stats = g.Scraper(g.SUBJECTS["physics"], str(tmp_path), session=session).run()
    assert stats["downloaded"] == 0 and stats["skipped"] == 4
    # existing files are skipped before any request is made for them
    for done in ("https://cdn.example/p1.pdf", "https://cdn.example/old/p1.pdf",
                 "https://docs.google.com/uc?export=download&id=IDONE"):
        assert done not in session.requested
    # ...while the download that failed last time is retried
    assert "https://docs.google.com/uc?export=download&id=IDTWO" in session.requested


def test_truncated_download_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "MAX_PDF_BYTES", 10_000)
    scraper = g.Scraper(g.SUBJECTS["physics"], str(tmp_path),
                        session=FakeSession({"https://cdn.example/big.pdf": PDF}))
    target = tmp_path / "out" / "big.pdf"
    scraper.download("https://cdn.example/big.pdf", str(target))
    assert not target.exists() and not (tmp_path / "out" / "big.pdf.part").exists()
    assert scraper.stats["failed"] == 1


def test_unreachable_listing_raises(tmp_path):
    scraper = g.Scraper(g.SUBJECTS["ict"], str(tmp_path), session=FakeSession({}))
    with pytest.raises(requests.HTTPError):
        scraper.run()


def test_launchers_wire_to_the_right_subject():
    import glob
    import re
    launchers = glob.glob(os.path.join(os.path.dirname(__file__), "..", "GCE Paper Scrapper (*).py"))
    keys = {re.search(r'main\("([^"]+)"\)', open(p).read()).group(1) for p in launchers}
    assert keys == set(g.SUBJECTS)
