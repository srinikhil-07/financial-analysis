# Financial analysis

[Read the SEC filing archive](https://srinikhil-07.github.io/financial-analysis/)

GitHub Actions monitors **US / AAPL (Apple Inc.)** twice daily, at 02:17 and
14:17 UTC. Scheduled runs can be delayed by GitHub. You can also use
**Actions → Archive SEC filings and publish reading room → Run workflow**.

The first successful run archives the latest **10-Q, 10-K and 8-K**, plus
available amendments for those report dates. Subsequent runs collect new filings
of these forms and their amendments, with a seven-day overlap. A missed run does
not advance the checkpoint; older submission pages are fetched when needed to
catch up after a long pause. Other SEC forms are not currently monitored.

## Reading the documents

Enable **Settings → Pages → Build and deployment → Source → GitHub Actions**.
The workflow publishes a company index linking directly to the original SEC HTML,
PDFs, exhibits, XBRL data and other files. HTML renders in the browser, PDFs use
the browser's PDF viewer, and other formats may display as text or download.

Original documents live in the repository at:

```text
reports/US/AAPL/
  archive.json
  sources/
    <accession-number>/
      <original-filing>.htm
      <original-exhibits-and-assets>
```

Each SEC filing-directory file is downloaded without rewriting its bytes or
filename. Relative links to included assets remain intact; absolute SEC links
still go to SEC.gov. The generated index is separate from the original documents.
`archive.json` records SEC source URLs, dates, filenames, byte counts and SHA-256
checksums. Amendments have their own accession directories and never overwrite
original filings. This is a filing archive, not yet financial analysis or a
quarter-by-quarter historical backfill.

**The Pages site is public.** Do not add confidential reports or other private
content to the published `reports/` tree.

## Adding a company

Edit `watchlist.json` on GitHub and commit:

```json
{
  "companies": [
    {"country": "US", "ticker": "AAPL", "cik": "0000320193"},
    {"country": "US", "ticker": "MSFT", "cik": "0000789019"}
  ]
}
```

Use the issuer's verified SEC CIK, zero-padded to ten digits, not a guessed ticker
mapping. `country` identifies the listing market; this implementation supports US
SEC issuers only. Foreign issuers using 20-F/6-K need additional form support.
Removing a company stops monitoring and removes it from the next site's index;
its original files remain in Git. Share classes of the same issuer have the same
CIK and would duplicate the archive if watched separately.

## Configuration and failures

Optionally set the Actions repository variable **SEC_USER_AGENT** to an application
name and a real maintainer contact email, per SEC fair-access guidance. By default,
the request identifies this application, repository and maintainer profile URL.
Requests are serialized below four per second with bounded retries for rate limits,
transient server errors and connection failures. No paid API or API key is needed.

The workflow needs Contents write permission to commit archives and Pages write
permission to deploy. Repository rules must allow its bot to push to `main`.
Failed downloads fail the run rather than publish an incomplete filing. Completed
filings are saved and committed even if a later download fails. Oversized files
(over 90 MiB) fail explicitly to avoid GitHub's per-file limit. Large archives may
eventually exceed GitHub Pages or repository size limits; do not use this as an
unbounded archive of all EDGAR filings.

SEC access restrictions can return HTTP 403 to cloud runners. Such failures are
visible in Actions and do not silently skip filings. Follow SEC access guidance;
do not bypass blocks. If a valid identifying User-Agent does not resolve a block,
use a permitted runner/network.

## Local use

Python 3.12 and its standard library are sufficient:

```sh
python -m unittest discover -s tests -v
export SEC_USER_AGENT="your-application/1.0 contact@example.com"
python scripts/sec_archive.py sync
python scripts/sec_archive.py build
python -m http.server --directory site 8000
```

The build requires a nonexistent output directory (default `site/`); use
`--output /path/to/new-directory` for a subsequent build. The generated site and
temporary downloads are ignored by Git.

Sources: [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and [SEC access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).
