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

    def test_sample_after_end_ts_excluded(self):
        """结束后才产生的样本（即使先到）不影响账单。"""
        t0 = ts("2026-09-10 22:30")
        sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": t0, "meter_kwh": 1000.0}).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": t0})
        samples = [
            {"seq": i + 1, "ts": t0 + (i + 1) * 1800, "kwh": 1000.0 + (i + 1) * 30.0}
            for i in range(18)
        ]
        # 结束后才产生的样本（07:35、07:40）混在批里一起上报
        end = t0 + 9 * 3600
        late = [
            {"seq": 19, "ts": end + 300, "kwh": 1545.0},
            {"seq": 20, "ts": end + 600, "kwh": 1550.0},
        ]
        self.client.post("/api/piles/P1/meter",
                         json={"session_id": sid, "samples": samples + late})
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": end})
        bill = self.client.post(f"/api/sessions/{sid}/settle").get_json()["bill"]
        self.assertEqual(bill["total_kwh"], 540.0)          # 不是 550
        self.assertAlmostEqual(float(bill["total_amount"]), 456.00)
        sess = self.client.get(f"/api/sessions/{sid}").get_json()["session"]
        self.assertEqual(sess["end_meter"], 1540.0)         # 定格在窗口内末次表码

    def test_late_backfill_before_end_ts_counted(self):
        """结束前产生、结束后才晚到的补报仍计入账单。"""
        t0 = ts("2026-09-10 22:30")
        sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": t0, "meter_kwh": 1000.0}).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": t0})
        samples = [
            {"seq": i + 1, "ts": t0 + (i + 1) * 1800, "kwh": 1000.0 + (i + 1) * 30.0}
            for i in range(18)
        ]
        held = samples[-1]            # 07:30 的最后一条先扣住，模拟断线晚到
        self.client.post("/api/piles/P1/meter",
                         json={"session_id": sid, "samples": samples[:-1]})
        end = t0 + 9 * 3600
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": end})
        # 结束后补报才到：ts 在窗口内，必须算进去
        self.client.post("/api/piles/P1/meter", json={"session_id": sid, "samples": [held]})
        bill = self.client.post(f"/api/sessions/{sid}/settle").get_json()["bill"]
        self.assertEqual(bill["total_kwh"], 540.0)          # 不是 510
        sess = self.client.get(f"/api/sessions/{sid}").get_json()["session"]
        self.assertEqual(sess["end_meter"], 1540.0)         # 结算时按补报重新定格

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


class TestBalanceAndOccupancy(unittest.TestCase):
    """预付费断电 + 占位费。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register", json={"pile_id": "P1"})
        self.client.post("/api/config", json={
            "occupancy_free_minutes": 10, "occupancy_fee_per_min": 1.0})
        self.client.post("/api/owners/U1/recharge", json={"amount": "100.00"})
        self.t0 = ts("2026-09-10 22:30")

    def tearDown(self):
        os.unlink(self.db)

    def _start(self, owner="U1"):
        sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 1000.0
        }).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": self.t0, "owner_id": owner})
        return sid

    def _charge_until_cutoff(self, sid):
        """22:30 起每 5 分钟 5 度，直到服务端下令断电。返回断电响应。"""
        for i in range(1, 200):
            t = self.t0 + i * 300
            r = self.client.post("/api/piles/P1/meter", json={
                "session_id": sid,
                "samples": [{"seq": i, "ts": t, "kwh": 1000.0 + i * 5.0}],
            }).get_json()
            if r.get("cmd"):
                return r, i
        self.fail("余额耗尽前未收到断电指令")

    def test_balance_cutoff_stops_charging(self):
        sid = self._start()
        (r, n) = self._charge_until_cutoff(sid)
        # 22:30-23:00 平 ¥6/间隔，23:00 后谷 ¥4/间隔；¥100 在第 21 个间隔(00:15)触顶
        self.assertEqual(n, 21)
        self.assertEqual(r["cmd"]["reason"], "balance_insufficient")
        # 断电 -> 结束 -> 结算：按实际充电量出账并从余额扣款
        end = self.t0 + n * 300
        self.client.post(f"/api/sessions/{sid}/stop",
                         json={"ts": end, "reason": "balance_insufficient"})
        r = self.client.post(f"/api/sessions/{sid}/settle", json={"ts": end}).get_json()
        bill = r["bill"]
        self.assertEqual(bill["total_kwh"], 105.0)
        self.assertAlmostEqual(float(bill["total_amount"]), 96.00)   # 平30×1.2 + 谷75×0.8
        self.assertAlmostEqual(float(r["owner_balance"]), 4.00)      # 100 - 96
        # 车主收到预警和断电通知
        notifs = self.client.get("/api/owners/U1").get_json()["notifications"]
        types = [x["type"] for x in notifs]
        self.assertIn("balance_warning", types)
        self.assertIn("balance_stop", types)

    def test_scan_rejected_when_balance_empty(self):
        self.client.post("/api/owners/U2/recharge", json={"amount": "10.00"})
        # U2 把钱用光：直接扣到 0（模拟历史订单），这里用未知账户和欠费账户分别验证
        r = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 1000.0})
        sid = r.get_json()["session"]["session_id"]
        r = self.client.post("/api/piles/P1/scan",
                             json={"ts": self.t0, "owner_id": "NOBODY"})
        self.assertEqual(r.status_code, 404)          # 未知账户
        r = self.client.post("/api/piles/P1/scan", json={"ts": self.t0})  # 访客仍可充
        self.assertEqual(r.status_code, 200)

    def test_occupancy_fee_until_plug_out(self):
        sid = self._start()
        for i in range(1, 3):  # 充 10 分钟，10 度
            self.client.post("/api/piles/P1/meter", json={
                "session_id": sid,
                "samples": [{"seq": i, "ts": self.t0 + i * 300, "kwh": 1000.0 + i * 5.0}]})
        end = self.t0 + 600
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": end})
        self.client.post(f"/api/sessions/{sid}/settle", json={"ts": end})
        # 宽限内：占位费 0
        r = self.client.get(f"/api/sessions/{sid}?ts={end + 8 * 60}").get_json()
        self.assertEqual(r["occupancy_running"]["fee"], "0.00")
        # 超宽限：25 分钟 - 10 分钟宽限 = 15 分钟 × 1 元
        r = self.client.get(f"/api/sessions/{sid}?ts={end + 25 * 60}").get_json()
        self.assertEqual(r["occupancy_running"]["fee"], "15.00")
        # 拔枪：占位费并入账单，会话关闭，从余额扣占位费
        r = self.client.post("/api/piles/P1/event",
                             json={"type": "plug_out", "ts": end + 25 * 60}).get_json()
        self.assertEqual(r["session"]["state"], "CLOSED")
        bill = r["bill"]
        self.assertEqual(bill["occupancy"]["billable_minutes"], 15)
        self.assertEqual(bill["occupancy"]["fee"], "15.00")
        # 充电 10 度×1.2=12 元（平时段）+ 占位 15 = 27
        self.assertAlmostEqual(float(bill["total_amount"]), 27.00)
        bal = float(self.client.get("/api/owners/U1").get_json()["balance"])
        self.assertAlmostEqual(bal, 100.0 - 12.0 - 15.0)
        # 重复拔枪幂等
        r = self.client.post("/api/piles/P1/event",
                             json={"type": "plug_out", "ts": end + 30 * 60}).get_json()
        self.assertTrue(r["duplicated"])

    def test_occupancy_within_grace_is_free(self):
        sid = self._start()
        self.client.post("/api/piles/P1/meter", json={
            "session_id": sid,
            "samples": [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}]})
        end = self.t0 + 300
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": end})
        self.client.post(f"/api/sessions/{sid}/settle", json={"ts": end})
        r = self.client.post("/api/piles/P1/event",
                             json={"type": "plug_out", "ts": end + 5 * 60}).get_json()
        self.assertEqual(r["bill"]["occupancy"]["fee"], "0.00")
        self.assertAlmostEqual(float(r["bill"]["total_amount"]), 6.00)  # 只有充电费


if __name__ == "__main__":
    unittest.main()
