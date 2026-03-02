import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================================
# CONFIG
# ===================================

START_URL = "https://cameroongcerevision.com/a-level/june-computer-science/"
SAVE_FOLDER = "GCE-CSC"

HEADERS = {"User-Agent": "Mozilla/5.0"}

MAX_WORKERS = 10

os.makedirs(SAVE_FOLDER, exist_ok=True)

visited_pages = set()
downloaded_files = set()

# FAST persistent session
session = requests.Session()
session.headers.update(HEADERS)

# ===================================
# FILTERS
# ===================================

def is_valid_math_page(url):
    url = url.lower()

    if "Computer" not in url:
        return False

    banned = [
        "chemistry", "biology", "economics",
        "geography", "commerce", "entrepreneur",
        "religious", "history", "math",
        "o-level"
    ]

    return not any(word in url for word in banned)


def fix_url(url):
    if url.startswith("//"):
        return "https:" + url
    return url


# ===================================
# GOOGLE DRIVE FIX
# ===================================

def convert_google_drive(url):
    if "docs.google.com" in url and "/d/" in url:
        try:
            file_id = url.split("/d/")[1].split("/")[0]
            return f"https://docs.google.com/uc?export=download&id={file_id}"
        except:
            pass
    return url


# ===================================
# YEAR DETECTION
# ===================================

def extract_year(text):
    match = re.search(r"(20\d{2})", text)
    return match.group(1) if match else "Unknown_Year"


# ===================================
# DOWNLOAD PDF
# ===================================

def download_pdf(pdf_url, source_page):

    pdf_url = convert_google_drive(fix_url(pdf_url))

    if pdf_url in downloaded_files:
        return

    print("Checking:", pdf_url)

    try:
        r = session.get(
            pdf_url,
            stream=True,
            allow_redirects=True,
            timeout=30
        )
    except Exception as e:
        print("❌ Request failed:", e)
        return

    # verify real PDF
    content_type = r.headers.get("Content-Type", "").lower()

    try:
        first_chunk = next(r.iter_content(2048))
    except StopIteration:
        return

    if not (
        "application/pdf" in content_type
        or "octet-stream" in content_type
        or first_chunk.startswith(b"%PDF")
    ):
        print("⚠ Skipped (not PDF)")
        return

    downloaded_files.add(pdf_url)

    filename = pdf_url.split("/")[-1].split("?")[0]
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    year = extract_year(source_page)
    year_folder = os.path.join(SAVE_FOLDER, year)
    os.makedirs(year_folder, exist_ok=True)

    filepath = os.path.join(year_folder, filename)

    print("⬇ Downloading:", filename)

    with open(filepath, "wb") as f:
        f.write(first_chunk)
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)


# ===================================
# EXTRACT PDFS FROM PAGE (FIXED)
# ===================================

def extract_pdfs(page_url):

    tasks = []

    if page_url in visited_pages:
        return tasks

    visited_pages.add(page_url)

    print("🔎 Scanning:", page_url)

    try:
        r = session.get(page_url, timeout=20)
    except:
        return tasks

    soup = BeautifulSoup(r.text, "html.parser")

    # --- Google viewer iframes ---
    for iframe in soup.find_all("iframe"):

        src = iframe.get("src", "")

        if "docs.google.com/gview" in src:

            if src.startswith("//"):
                src = "https:" + src

            parsed = urlparse(src)
            query = parse_qs(parsed.query)

            pdf = query.get("url", [None])[0]

            if pdf:
                tasks.append((pdf, page_url))

    # --- direct PDF links fallback ---
    for a in soup.find_all("a", href=True):
        link = a["href"]
        if ".pdf" in link.lower():
            tasks.append((urljoin(page_url, link), page_url))

    return tasks


# ===================================
# MAIN SCRAPER (PARALLEL)
# ===================================

def scrape_subject():

    res = session.get(START_URL)
    soup = BeautifulSoup(res.text, "html.parser")

    paper_pages = []

    articles = soup.find_all("article")

    for article in articles:
        for a in article.find_all("a", href=True):
            link = urljoin(START_URL, a["href"])

            if is_valid_math_page(link):
                paper_pages.append(link)

    all_download_tasks = []

    # parallel scanning
    with ThreadPoolExecutor(MAX_WORKERS) as executor:
        futures = [executor.submit(extract_pdfs, page) for page in paper_pages]

        for future in as_completed(futures):
            result = future.result()
            if result:
                all_download_tasks.extend(result)

    # parallel downloading
    with ThreadPoolExecutor(MAX_WORKERS) as executor:
        futures = [
            executor.submit(download_pdf, url, page)
            for url, page in all_download_tasks
        ]

        for _ in as_completed(futures):
            pass


# ===================================
# RUN
# ===================================

scrape_subject()

print("\n✅ DONE — FAST DOWNLOAD COMPLETE")