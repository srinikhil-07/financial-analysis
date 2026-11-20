#!/usr/bin/env python3
"""Archive original SEC filings and build a static reading index."""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "8-K", "8-K/A"}
MAX_FILE_BYTES = 90 * 1024 * 1024
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def filename(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"Invalid SEC filename: {value!r}")
    if "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"Unsafe SEC filename: {value!r}")
    return value


def load_companies(path):
    companies = read_json(path)["companies"]
    seen = set()
    for company in companies:
        country, ticker, cik = (company[key] for key in ("country", "ticker", "cik"))
        if country != "US":
            raise ValueError("This SEC monitor currently supports country US only")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", ticker):
            raise ValueError(f"Invalid ticker: {ticker!r}")
        if not isinstance(cik, str) or not re.fullmatch(r"\d{10}", cik):
            raise ValueError(f"{ticker}: CIK must be a zero-padded, 10-digit string")
        if (country, ticker) in seen:
            raise ValueError(f"Duplicate company: {country}/{ticker}")
        seen.add((country, ticker))
    return companies


class SECClient:
    def __init__(self, user_agent):
        if not user_agent.strip():
            raise ValueError("SEC_USER_AGENT must identify this application and its maintainer")
        self.user_agent = user_agent
        self.last_request = 0.0

    def download(self, url, target):
        if not url.startswith(("https://www.sec.gov/", "https://data.sec.gov/")):
            raise ValueError(f"Not an SEC URL: {url}")
        for attempt in range(4):
            time.sleep(max(0, 0.3 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = Request(url, headers={"User-Agent": self.user_agent})
            try:
                with urlopen(request, timeout=90) as response:
                    digest = hashlib.sha256()
                    size = 0
                    with target.open("wb") as output:
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_FILE_BYTES:
                                raise ValueError(f"File exceeds 90 MiB Git archive limit: {url}")
                            output.write(chunk)
                            digest.update(chunk)
                return {"bytes": size, "sha256": digest.hexdigest()}
            except HTTPError as error:
                if error.code == 403:
                    raise RuntimeError(
                        f"SEC denied access to {url} (HTTP 403). Configure SEC_USER_AGENT "
                        "with a real maintainer contact. If access remains denied, use "
                        "an SEC-permitted runner/network; do not bypass the block."
                    ) from error
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                print(f"SEC HTTP {error.code}; retrying {url}", file=sys.stderr)
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise
                print(f"SEC connection failed; retrying {url}", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"Download failed: {url}")

    def json(self, url):
        # A temporary directory also keeps API responses out of the committed archive.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "response.json"
            self.download(url, target)
            return read_json(target)


def filing_rows(columns):
    count = len(columns["accessionNumber"])
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    if any(len(columns[key]) != count for key in keys):
        raise ValueError("SEC submissions columns have inconsistent lengths")
    return [{key: columns[key][index] for key in keys} for index in range(count)]


def select_filings(rows, archived, checked_at):
    eligible = [row for row in rows if row["form"] in FORMS]
    if checked_at:
        # Overlap catches delayed submissions and rechecks the last successful day.
        cutoff = (date.fromisoformat(checked_at[:10]) - timedelta(days=7)).isoformat()
        selected = [row for row in eligible if row["filingDate"] >= cutoff]
    else:
        latest = {}
        for row in sorted(eligible, key=lambda row: (row["filingDate"], row["accessionNumber"])):
            if not row["form"].endswith("/A"):
                latest[row["form"]] = row
        selected = list(latest.values())
        periods = {(row["form"], row["reportDate"]) for row in selected}
        selected.extend(
            row for row in eligible
            if row["form"].endswith("/A")
            and (row["form"][:-2], row["reportDate"]) in periods
        )
    unique = {row["accessionNumber"]: row for row in selected}
    return sorted(
        (row for accession, row in unique.items() if accession not in archived),
        key=lambda row: (row["filingDate"], row["accessionNumber"]),
    )


def fetch_rows(client, submissions, checked_at):
    rows = filing_rows(submissions["filings"]["recent"])
    cutoff = (
        (date.fromisoformat(checked_at[:10]) - timedelta(days=7)).isoformat()
        if checked_at else None
    )
    # Fresh companies need only the latest annual, quarterly and current reports.
    # After an extended pause, fetch older submission pages rather than lose filings.
    for page in submissions["filings"].get("files", []):
        need_bootstrap = not checked_at and not {"10-Q", "10-K", "8-K"}.issubset(
            {row["form"] for row in rows}
        )
        if need_bootstrap or (cutoff and page["filingTo"] >= cutoff):
            name = filename(page["name"])
            rows.extend(filing_rows(client.json(f"https://data.sec.gov/submissions/{name}")))
    return rows


def archive_filing(client, company, row, company_dir, staging_root):
    accession = row["accessionNumber"]
    if not ACCESSION.fullmatch(accession):
        raise ValueError(f"Invalid accession number: {accession!r}")
    primary = filename(row["primaryDocument"])
    cik = str(int(company["cik"]))
    source_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    listing = client.json(f"{source_url}/index.json")
    names = [filename(item["name"]) for item in listing["directory"]["item"]]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate filenames in {accession}")
    if primary not in names:
        raise ValueError(f"Primary document missing from SEC directory: {primary}")
    staging = staging_root / company["ticker"] / accession
    staging.mkdir(parents=True, exist_ok=True)
    documents = []
    try:
        for name in sorted(names):
            print(f"Downloading {company['ticker']} {accession}/{name}", flush=True)
            integrity = client.download(f"{source_url}/{quote(name, safe='')}", staging / name)
            documents.append({"name": name, **integrity})
        destination = company_dir / "sources" / accession
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(f"Unindexed archive directory already exists: {destination}")
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {**row, "source_url": source_url, "documents": documents}


def sync(root, client):
    started = datetime.now(timezone.utc).isoformat()
    for company in load_companies(root / "watchlist.json"):
        company_dir = root / "reports" / company["country"] / company["ticker"]
        manifest_path = company_dir / "archive.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {
            **company, "filings": {}, "checked_at": None,
        }
        if any(manifest[key] != company[key] for key in ("country", "ticker", "cik")):
            raise ValueError("Watchlist identity changed; use a separate archive path")
        submissions = client.json(
            f"https://data.sec.gov/submissions/CIK{company['cik']}.json"
        )
        if int(submissions["cik"]) != int(company["cik"]):
            raise ValueError("SEC response CIK does not match watchlist")
        manifest["name"] = submissions["name"]
        rows = fetch_rows(client, submissions, manifest["checked_at"])
        for row in select_filings(rows, manifest["filings"], manifest["checked_at"]):
            archived = archive_filing(
                client, company, row, company_dir, root / ".archive-staging"
            )
            manifest["filings"][row["accessionNumber"]] = archived
            # Save each completed filing so interrupted runs can resume safely.
            write_json(manifest_path, manifest)
        manifest["checked_at"] = started
        write_json(manifest_path, manifest)


STYLE = """
body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}
a{color:#0969da}table{border-collapse:collapse;width:100%}td,th{text-align:left;
padding:.7rem;border-bottom:1px solid #ddd;vertical-align:top}small{color:#555}
code{overflow-wrap:anywhere}summary{cursor:pointer}h1{line-height:1.2}
"""


def page(title, content):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style>"
        f"<body><h1>{html.escape(title)}</h1>{content}</body></html>"
    )


def build_site(root, output):
    if output.resolve() == root.resolve() or root.resolve() in output.resolve().parents:
        if output.resolve() != (root / "site").resolve():
            raise ValueError("Build output inside the repository must be the site directory")
    if output.exists():
        raise ValueError(f"Build output already exists; choose an empty destination: {output}")
    output.mkdir(parents=True)
    links = []
    for company in load_companies(root / "watchlist.json"):
        relative = Path("reports") / company["country"] / company["ticker"]
        directory = root / relative
        if not (directory / "archive.json").exists():
            raise ValueError(f"No archive for {company['ticker']}; run sync first")
        manifest = read_json(directory / "archive.json")
        shutil.copytree(directory, output / relative)
        rows = []
        for filing in sorted(
            manifest["filings"].values(),
            key=lambda row: (row["filingDate"], row["accessionNumber"]),
            reverse=True,
        ):
            prefix = f"sources/{filing['accessionNumber']}/"
            primary = quote(filing["primaryDocument"], safe="")
            documents = "".join(
                f'<li><a href="{prefix}{quote(doc["name"], safe="")}">'
                f'{html.escape(doc["name"])}</a> ({doc["bytes"]:,} bytes)</li>'
                for doc in filing["documents"]
            )
            rows.append(
                f'<tr><td>{html.escape(filing["form"])}</td>'
                f'<td>{html.escape(filing["filingDate"])}</td>'
                f'<td>{html.escape(filing["reportDate"])}</td>'
                f'<td><a href="{prefix}{primary}">Read original</a> · '
                f'<a href="{html.escape(filing["source_url"], quote=True)}/{primary}">'
                'SEC source</a>'
                f'<details><summary>All {len(filing["documents"])} files</summary>'
                f'<ul>{documents}</ul></details></td></tr>'
            )
        title = f'{company["country"]} / {company["ticker"]} — {manifest["name"]}'
        content = (
            '<p><a href="../../../index.html">All companies</a></p>'
            '<p>Original SEC files, preserved without conversion. Dates are filing and '
            'report dates, not calendar-quarter labels. Includes 10-Q, 10-K, 8-K and '
            'their amendments; this is not a complete historical archive.</p>'
            f'<p><small>Last successful check: {html.escape(manifest["checked_at"])}</small></p>'
            '<table><thead><tr><th>Form</th><th>Filed</th><th>Period / event date</th>'
            f'<th>Documents</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )
        (output / relative / "index.html").write_text(page(title, content), encoding="utf-8")
        links.append(
            f'<li><a href="{relative.as_posix()}/index.html">'
            f'{html.escape(title)}</a> — {len(rows)} filings</li>'
        )
    (output / "index.html").write_text(
        page("SEC filing reading room", "<p>Public SEC disclosures, archived automatically.</p>"
             f'<ul>{"".join(links)}</ul>'),
        encoding="utf-8",
    )
    (output / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "build"])
    parser.add_argument("--output", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    if args.command == "sync":
        sync(ROOT, SECClient(os.environ.get("SEC_USER_AGENT", "")))
    else:
        build_site(ROOT, args.output)


if __name__ == "__main__":
    main()
