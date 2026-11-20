import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "sec_archive", Path(__file__).resolve().parents[1] / "scripts" / "sec_archive.py"
)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)

COMPANY = {"country": "US", "ticker": "AAPL", "cik": "0000320193"}


def filing(number, form, filed, period="2026-09-26"):
    return {
        "accessionNumber": f"0000320193-26-{number:06}",
        "form": form,
        "filingDate": filed,
        "reportDate": period,
        "primaryDocument": "aapl.htm",
    }


def columns(rows):
    return {key: [row[key] for row in rows] for key in rows[0]}


class FakeClient:
    def __init__(self, submissions=None, fail=None):
        self.submissions = submissions
        self.fail = fail
        self.files = {
            "aapl.htm": b'<html><img src="image.png">Original SEC HTML</html>\r\n',
            "image.png": b"\x89PNG\r\n\x1a\noriginal",
        }
        self.downloads = []

    def json(self, url):
        if url.endswith("/index.json"):
            return {"directory": {"item": [{"name": name} for name in self.files]}}
        return self.submissions

    def download(self, url, target):
        name = url.rsplit("/", 1)[1]
        self.downloads.append(name)
        if name == self.fail:
            raise RuntimeError("Simulated SEC failure")
        payload = self.files[name]
        target.write_bytes(payload)
        return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


class FilingSelectionTests(unittest.TestCase):
    def test_bootstrap_latest_base_forms_and_matching_amendments(self):
        rows = [
            filing(1, "10-Q", "2026-05-01", "2026-03-28"),
            filing(2, "10-Q", "2026-08-01", "2026-06-27"),
            filing(3, "10-K", "2026-10-30"),
            filing(4, "8-K", "2026-10-29"),
            filing(5, "10-Q/A", "2026-08-02", "2026-06-27"),
            filing(6, "4", "2026-11-01"),
            filing(7, "10-Q/A", "2026-05-02", "2026-03-28"),
        ]
        result = archive.select_filings(rows, {}, None)
        self.assertEqual({row["accessionNumber"] for row in result},
                         {rows[index]["accessionNumber"] for index in (1, 2, 3, 4)})

    def test_incremental_overlap_amendments_and_deduplication(self):
        rows = [
            filing(1, "10-K", "2026-10-30"),
            filing(2, "10-K/A", "2026-11-15"),
            filing(3, "8-K", "2026-11-12"),
            filing(4, "4", "2026-11-19"),
        ]
        result = archive.select_filings(
            rows + [rows[1]], {rows[2]["accessionNumber"]: {}}, "2026-11-19T02:17:00+00:00"
        )
        self.assertEqual(result, [rows[1]])

    def test_fetches_historical_submission_pages_after_long_pause(self):
        recent = filing(3, "8-K", "2026-11-19")
        older = filing(2, "10-Q", "2026-08-01")
        submissions = {"filings": {"recent": columns([recent]), "files": [
            {"name": "older.json", "filingTo": "2026-09-01"}
        ]}}
        result = archive.fetch_rows(
            FakeClient(columns([older])), submissions, "2026-07-01T00:00:00+00:00"
        )
        self.assertEqual(result, [recent, older])


class ArchiveTests(unittest.TestCase):
    def test_sync_preserves_bytes_is_idempotent_and_builds_links(self):
        row = filing(1, "10-Q", "2026-11-19")
        client = FakeClient({
            "cik": 320193, "name": "Apple & Company",
            "filings": {"recent": columns([row]), "files": []},
        })
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive.write_json(root / "watchlist.json", {"companies": [COMPANY]})
            archive.sync(root, client)
            archive.sync(root, client)
            directory = root / "reports/US/AAPL"
            self.assertEqual(client.downloads, ["aapl.htm", "image.png"])
            self.assertEqual(
                (directory / "sources" / row["accessionNumber"] / "aapl.htm").read_bytes(),
                client.files["aapl.htm"],
            )
            manifest = archive.read_json(directory / "archive.json")
            saved = manifest["filings"][row["accessionNumber"]]
            self.assertEqual(saved["documents"][0]["sha256"],
                             hashlib.sha256(client.files["aapl.htm"]).hexdigest())
            output = root / "site"
            archive.build_site(root, output)
            index = (output / "reports/US/AAPL/index.html").read_text()
            self.assertIn(f'sources/{row["accessionNumber"]}/aapl.htm', index)
            self.assertIn("Apple &amp; Company", index)
            self.assertTrue((output / ".nojekyll").exists())
            self.assertEqual(
                (output / "reports/US/AAPL/sources" / row["accessionNumber"] / "image.png")
                .read_bytes(), client.files["image.png"],
            )

    def test_partial_download_never_becomes_a_completed_archive(self):
        client = FakeClient(fail="image.png")
        row = filing(1, "10-Q", "2026-11-19")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "Simulated SEC failure"):
                archive.archive_filing(client, COMPANY, row, root / "company", root / "stage")
            self.assertFalse((root / "company/sources" / row["accessionNumber"]).exists())
            self.assertFalse((root / "stage/AAPL" / row["accessionNumber"]).exists())

    def test_failed_sync_keeps_completed_filings_without_advancing_checkpoint(self):
        first = filing(1, "10-Q", "2026-11-18")
        second = filing(2, "8-K", "2026-11-19")
        second["primaryDocument"] = "missing.htm"
        client = FakeClient({
            "cik": 320193, "name": "Apple",
            "filings": {"recent": columns([first, second]), "files": []},
        })
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive.write_json(root / "watchlist.json", {"companies": [COMPANY]})
            with self.assertRaisesRegex(ValueError, "Primary document missing"):
                archive.sync(root, client)
            manifest = archive.read_json(root / "reports/US/AAPL/archive.json")
            self.assertIsNone(manifest["checked_at"])
            self.assertEqual(list(manifest["filings"]), [first["accessionNumber"]])
            client.files["missing.htm"] = b"Second filing"
            archive.sync(root, client)
            manifest = archive.read_json(root / "reports/US/AAPL/archive.json")
            self.assertIsNotNone(manifest["checked_at"])
            self.assertEqual(len(manifest["filings"]), 2)

    def test_rejects_directory_traversal_from_sec_listing(self):
        client = FakeClient()
        client.files["../escape.htm"] = b"bad"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "Unsafe SEC filename"):
                archive.archive_filing(
                    client, COMPANY, filing(1, "10-Q", "2026-11-19"),
                    root / "company", root / "stage",
                )
            self.assertEqual(client.downloads, [])


if __name__ == "__main__":
    unittest.main()
