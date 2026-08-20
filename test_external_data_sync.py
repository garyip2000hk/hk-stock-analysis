import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from external_data_sync import (
    fetch_bytes,
    parse_cr_weekly_text,
    parse_sfc_short_positions,
    sync_short_positions,
)


SFC_FIXTURE = """Date,Stock Code,Stock Name,Aggregated Reportable Short Positions (Shares),Aggregated Reportable Short Positions (HK$)
07/08/2026,1,CKH HOLDINGS,42,4200
"""

CR_FIXTURE = """1. Example New Company Limited      81043885     14-08-2026      Nil
2. Example Renamed Company Limited  81043886     Nil 12-08-2026
"""


class ExternalDataSyncTests(unittest.TestCase):
    def test_parses_official_sfc_csv_into_existing_short_position_schema(self):
        records = parse_sfc_short_positions(SFC_FIXTURE)

        self.assertEqual(records[0]["date"], "2026-08-07")
        self.assertEqual(records[0]["code"], "00001")
        self.assertEqual(records[0]["short_hkd"], 4200)

    def test_fetch_uses_a_browser_compatible_user_agent(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b"ok"

        def fake_open(request, timeout):
            captured["user_agent"] = request.get_header("User-agent")
            return Response()

        with patch("external_data_sync.urlopen", fake_open):
            self.assertEqual(fetch_bytes("https://example.test", attempts=1), b"ok")

        self.assertIn("Mozilla", captured["user_agent"])

    def test_short_sync_merges_latest_public_data_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "short_positions.json").write_text(
                '[{"date":"2026-08-01","code":"00001","name":"OLD","short_shares":1,"short_hkd":10}]',
                encoding="utf-8",
            )
            result = sync_short_positions(output_dir, fetcher=lambda _: SFC_FIXTURE.encode())
            merged = (output_dir / "short_positions.json").read_text(encoding="utf-8")

            self.assertEqual(result["latest_date"], "2026-08-07")
            self.assertIn("2026-08-01", merged)
            self.assertIn("2026-08-07", merged)

    def test_parses_new_incorporations_and_name_changes_from_cr_weekly_text(self):
        signals = parse_cr_weekly_text(CR_FIXTURE, "https://example.test/report.pdf")

        self.assertEqual({item["type"] for item in signals}, {"new_incorporation", "name_change"})
        self.assertEqual({item["date"] for item in signals}, {"2026-08-14", "2026-08-12"})


if __name__ == "__main__":
    unittest.main()
