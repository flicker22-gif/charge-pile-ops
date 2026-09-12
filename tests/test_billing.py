"""计费核心逻辑测试：跨时段拆分、断线补报不丢电、结算幂等。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "server"))

import tariff  # noqa: E402
import app as server  # noqa: E402


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


class TestTariffSplit(unittest.TestCase):
    def test_single_period(self):
        r = tariff.split_by_period(ts("2026-09-10 10:00"), ts("2026-09-10 11:00"))
        self.assertEqual(r, {"峰": 3600.0})

    def test_cross_period_and_midnight(self):
        # 22:30 -> 次日 07:30：平0.5h + 谷8h + 平0.5h
        r = tariff.split_by_period(ts("2026-09-10 22:30"), ts("2026-09-11 07:30"))
        self.assertAlmostEqual(r["平"], 3600.0)
        self.assertAlmostEqual(r["谷"], 8 * 3600.0)
        self.assertAlmostEqual(sum(r.values()), 9 * 3600.0)

    def test_full_day_covers_24h(self):
        r = tariff.split_by_period(ts("2026-09-10 00:00"), ts("2026-09-11 00:00"))
        self.assertAlmostEqual(sum(r.values()), 24 * 3600.0)


class TestSettle(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register", json={"pile_id": "P1"})

    def tearDown(self):
        os.unlink(self.db)

    def _charge_session(self):
        """插枪->扫码->上报表码(含补报乱序/重发)->结束，返回 session_id。"""
        t0 = ts("2026-09-10 22:30")
        sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": t0, "meter_kwh": 1000.0}).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": t0})
        # 每小时 60 度，22:30 -> 07:30 共 540 度；模拟断线补报：乱序 + 重发
        samples = [
            {"seq": i + 1, "ts": t0 + (i + 1) * 1800, "kwh": 1000.0 + (i + 1) * 30.0}
            for i in range(18)
        ]
        shuffled = samples[:6] + samples[12:] + samples[6:12]  # 后 1/3 像补报一样晚到
        self.client.post("/api/piles/P1/meter", json={"session_id": sid, "samples": shuffled})
        r = self.client.post("/api/piles/P1/meter",
                             json={"session_id": sid, "samples": shuffled}).get_json()
        self.assertEqual(r["accepted"], 0)          # 整批重发全部被去重
        self.assertEqual(r["duplicated"], 18)
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": t0 + 9 * 3600})
        return sid

    def test_bill_splits_periods(self):
        sid = self._charge_session()
        bill = self.client.post(f"/api/sessions/{sid}/settle").get_json()["bill"]
        self.assertEqual(bill["total_kwh"], 540.0)
        by = {i["period"]: i for i in bill["breakdown"]}
        self.assertAlmostEqual(by["谷"]["kwh"], 480.0)   # 23:00-07:00
        self.assertAlmostEqual(by["平"]["kwh"], 60.0)    # 22:30-23:00 + 07:00-07:30
        self.assertAlmostEqual(float(bill["total_amount"]), 456.00)

    def test_settle_is_idempotent(self):
        sid = self._charge_session()
        b1 = self.client.post(f"/api/sessions/{sid}/settle").get_json()["bill"]
        r2 = self.client.post(f"/api/sessions/{sid}/settle").get_json()
        self.assertTrue(r2["duplicated"])
        self.assertEqual(r2["bill"]["bill_id"], b1["bill_id"])

    def test_scan_and_plug_in_idempotent(self):
        t0 = ts("2026-09-10 22:30")
        r1 = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": t0, "meter_kwh": 1000.0}).get_json()
        r2 = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": t0, "meter_kwh": 1000.0}).get_json()
        self.assertEqual(r1["session"]["session_id"], r2["session"]["session_id"])
        sid = r1["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": t0})
        r = self.client.post("/api/piles/P1/scan", json={"ts": t0}).get_json()
        self.assertTrue(r["duplicated"])
        self.assertEqual(r["session"]["session_id"], sid)


if __name__ == "__main__":
    unittest.main()
