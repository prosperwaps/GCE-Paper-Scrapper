# Security

This is a command-line scraper that fetches web pages and saves PDFs to disk. It has no server, accounts, secrets or database, so the attack surface is: **everything it downloads and every URL it is told to follow comes from a remote site we don't control.** This document records the threat model, what was reviewed, and what is deliberately left open.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (Security → Report a vulnerability) rather than a public issue.

## Threat model

| Asset | Threat actor | Goal |
|---|---|---|
| The user's filesystem | A malicious or compromised page / PDF host | Write outside the download folder, overwrite files, fill the disk |
| The user's network position | Same | Make the scraper hit internal services (SSRF) |
| The user's machine | Same | Deliver a malicious PDF the user later opens |
| Availability of the source site | The user (accidentally) | Hammer a small site with parallel requests |

## Review findings

Status: **Fixed** = changed and covered by a test in `tests/test_gce_scraper.py`; **Mitigated** = reduced, residual risk noted; **Accepted** = out of scope, documented.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | **Path traversal via remote filenames.** Filenames were taken straight from URLs. Percent-encoded separators (`..%2F..%2F`), backslashes, NUL/control characters, leading dots and very long names could escape the target folder, create hidden files or crash the write. | High | **Fixed** — `safe_filename()` URL-decodes first, keeps only `[\w.\-() ]`, strips leading dots, caps length, and always appends `.pdf`. The year folder can only be 4 digits or `Unknown_Year`. Tests assert every output path stays inside the save folder. |
| 2 | **Silent overwrite / data loss.** Every Google Drive file was named `uc.pdf`; same-named PDFs from different hosts overwrote each other. | Medium | **Fixed** — Drive files use `gdrive-<id>.pdf`; `plan_downloads()` resolves collisions deterministically with a URL hash before any thread starts. |
| 3 | **Host checks by substring.** `"docs.google.com" in url` is true for `https://evil.example/?x=docs.google.com/d/…`. | Medium | **Fixed** — hosts are compared exactly via `urlparse().hostname`; iframe `gview` links on other hosts are ignored. |
| 4 | **Crawl scope.** Any link inside an `<article>` was followed, including off-site ones, and the scheme wasn't checked. | Medium | **Fixed** — pages must be `http(s)` on `cameroongcerevision.com` (or a subdomain, exact match, so `cameroongcerevision.com.evil.example` is rejected). Non-`http(s)` download URLs (`file:`, `ftp:`) are dropped. |
| 5 | **Unbounded download size.** A hostile server could stream forever. | Medium | **Fixed** — hard cap of 200 MB per file (counted after transport decompression); the partial file is deleted. Every request has a timeout. |
| 6 | **Corrupt partial files.** An interrupted download left a truncated PDF that later runs treated as "already downloaded". | Medium | **Fixed** — streamed to `<name>.pdf.part`, moved into place with `os.replace` only on success, removed on any failure. |
| 7 | **Weak "is it a PDF" check.** Any `octet-stream` response was accepted. | Low | **Fixed** — the `%PDF` signature must appear in the first KB. |
| 8 | **Data races.** Shared sets/counters were mutated from many threads. | Low | **Fixed** — state is per-`Scraper`, counters are lock-protected, the work list is computed single-threaded. |
| 9 | **Unhandled network errors** crashed with a traceback (and ran on import). | Low | **Fixed** — failures are counted and reported; the CLI exits non-zero with one clear line; nothing runs on import. |
| 10 | **Blind SSRF.** Paper links may legitimately point to third-party hosts (Google Drive, CDNs), so downloads can't be pinned to one host. A hostile page could point at `http://169.254.169.254/` or `localhost`. Requests are GET-only and a response is only kept if it is a PDF, so nothing is exfiltrated, but the request is still sent. | Low | **Accepted** — filtering literal IPs doesn't stop DNS rebinding or redirects, so a partial block would give false confidence. If you run this against sources you don't trust, run it in a container or VM without access to internal networks. |
| 11 | **Malicious PDFs.** The `%PDF` check proves the file *looks like* a PDF, not that it is safe. The scraper never parses or opens PDFs. | Low | **Accepted** — open downloads in an up-to-date viewer; consider the browser's built-in viewer or a sandbox for files from untrusted sources. |
| 12 | **TLS downgrade via redirect.** `allow_redirects` follows `https → http`. Certificates are always verified (`verify` is never disabled). | Low | **Accepted** — the source's own redirects are followed; a hostile redirect only causes a PDF-signature-checked download. |
| 13 | **Politeness / terms of use.** Default 10 parallel workers, generic `Mozilla/5.0` user agent, `robots.txt` not consulted. | Info | **Mitigated** — retries use exponential backoff on 429/5xx; use `--workers 2` to be gentler. Past papers may be copyrighted: download for personal study and respect the site's terms. |

## Supply chain and CI

- Dependencies are bounded to major versions in `requirements.txt` (`requests`, `beautifulsoup4`); no other runtime packages.
- The workflow runs with `permissions: contents: read` and no secrets. Actions are pinned to major versions (`@v4`/`@v5`), not commit SHAs — pin to SHAs if this repo ever gains secrets or release automation.
- The old `django.yml` workflow was removed: it referenced files that don't exist (`manage.py`, `requirements.txt`) and Python 3.7, which is end-of-life.
- Recommended next step: enable Dependabot (security updates) and run `pip-audit` in CI.

## Not applicable

No authentication, sessions, cookies, user input rendered as HTML, databases, secrets or deserialisation of untrusted data (`html.parser` is used for parsing; nothing is `eval`'d or unpickled).
