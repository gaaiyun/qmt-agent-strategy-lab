# coding: utf-8
import json
import sys
import tempfile
import datetime
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class BatchResearchTests(unittest.TestCase):
    def test_grid_has_multiple_horizons(self):
        from run_batch_research import _grid

        windows = {(row["short_window"], row["mid_window"], row["long_window"]) for row in _grid()}
        self.assertGreaterEqual(len(windows), 3)

    def test_cache_loader_aligns_all_symbols_to_defensive_dates(self):
        from run_batch_research import _load_cache

        dates = [(datetime.date(2024, 1, 1) + datetime.timedelta(days=day)).isoformat() for day in range(300)]
        rows = [{"date": date, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0 + index * 0.01, "volume": 1000} for index, date in enumerate(dates)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps({
                "510300.SH": rows,
                "510500.SH": rows,
                "159915.SZ": rows,
                "511010.SH": rows,
            }), encoding="utf-8")
            bars, common, risk = _load_cache(path)
        self.assertEqual(len(common), 300)
        self.assertEqual(len(risk), 3)
        self.assertEqual(set(bars), {"510300.SH", "510500.SH", "159915.SZ", "511010.SH"})


if __name__ == "__main__":
    unittest.main()
