import tempfile
import unittest
from pathlib import Path

from cbbc_warrants_importer import find_latest_snapshot, read_csv_with_retry


class CbbcResilienceTests(unittest.TestCase):
    def test_uses_latest_snapshot_when_today_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "cbbc_20260811.parquet").touch()
            (folder / "cbbc_20260812.parquet").touch()

            self.assertEqual(
                find_latest_snapshot(folder, "cbbc"),
                folder / "cbbc_20260812.parquet",
            )

    def test_retries_temporary_download_failure_before_succeeding(self):
        calls = []

        def flaky_reader(_: str):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("HTTP 503 Service Unavailable")
            return "downloaded"

        result = read_csv_with_retry(
            "https://example.test/cbbc.csv",
            "CBBC",
            reader=flaky_reader,
            attempts=3,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(result, "downloaded")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()

