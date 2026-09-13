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


class TestMeterClassification(unittest.TestCase):
    """上报接口逐条分类：accepted / duplicate / late。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register", json={"pile_id": "P1"})
        self.t0 = ts("2026-09-10 22:30")
        self.sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 1000.0
        }).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": self.t0})

    def tearDown(self):
        os.unlink(self.db)

    def _send(self, samples):
        return self.client.post("/api/piles/P1/meter",
                                json={"session_id": self.sid, "samples": samples}).get_json()

    def test_classification(self):
        r = self._send([{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0},
                        {"seq": 2, "ts": self.t0 + 600, "kwh": 1010.0}])
        self.assertEqual([x["status"] for x in r["results"]], ["accepted", "accepted"])
        self.assertEqual(r["summary"], {"accepted": 2, "duplicate": 0, "late": 0, "rejected": 0})
        # 重发 -> duplicate（兼容字段也在）
        r = self._send([{"seq": 2, "ts": self.t0 + 600, "kwh": 1010.0}])
        self.assertEqual(r["results"][0]["status"], "duplicate")
        self.assertEqual(r["duplicated"], 1)
        # 结束后才产生的样本 -> late（只存档不计费）
        end = self.t0 + 600
        self.client.post(f"/api/sessions/{self.sid}/stop", json={"ts": end})
        r = self._send([{"seq": 3, "ts": end + 300, "kwh": 1015.0}])
        self.assertEqual(r["results"][0]["status"], "late")
        self.assertIn("晚于结束时间", r["results"][0]["reason"])
        self.assertEqual(r["late"], 1)
        # 结束前产生、晚到的补报仍是 accepted
        r = self._send([{"seq": 4, "ts": self.t0 + 450, "kwh": 1007.5}])
        self.assertEqual(r["results"][0]["status"], "accepted")
        # 账单不受 late 样本影响：窗口内 1000->1007.5->1010 = 10 度
        bill = self.client.post(f"/api/sessions/{self.sid}/settle",
                                json={"ts": end}).get_json()["bill"]
        self.assertEqual(bill["total_kwh"], 10.0)


class TestStationTariff(unittest.TestCase):
    """场站自定义电价 + 版本化生效 + 历史账单冻结。"""

    PERIODS_V1 = [["00:00", "07:00", "谷", 0.30], ["07:00", "08:00", "平", 0.75],
                  ["08:00", "11:00", "峰", 1.20], ["11:00", "18:00", "平", 0.75],
                  ["18:00", "21:00", "峰", 1.20], ["21:00", "23:00", "平", 0.75],
                  ["23:00", "24:00", "谷", 0.30]]

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register",
                         json={"pile_id": "P1", "station_id": "S1"})
        self.t0 = ts("2026-09-10 22:30")

    def tearDown(self):
        os.unlink(self.db)

    def _set_tariff(self, valley_price, eff_ts):
        periods = [p[:] for p in self.PERIODS_V1]
        for p in periods:
            if p[2] == "谷":
                p[3] = valley_price
        return self.client.post("/api/stations/S1/tariff", json={
            "periods": periods, "service_fee": 0.45, "effective_ts": eff_ts})

    def _charge(self, start, hours, kwh_per_hour=60.0):
        sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": start, "meter_kwh": 1000.0
        }).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": start})
        n = int(hours * 2)  # 每 30 分钟一条
        samples = [{"seq": i + 1, "ts": start + (i + 1) * 1800,
                    "kwh": 1000.0 + (i + 1) * kwh_per_hour / 2} for i in range(n)]
        self.client.post("/api/piles/P1/meter", json={"session_id": sid, "samples": samples})
        end = start + hours * 3600
        self.client.post(f"/api/sessions/{sid}/stop", json={"ts": end})
        return sid, end

    def test_mid_session_price_change_splits_bill(self):
        self._set_tariff(0.30, self.t0 - 3600)                # 谷 0.30
        self._set_tariff(0.50, ts("2026-09-10 23:30"))        # 23:30 起谷 0.50
        sid, end = self._charge(self.t0, 2)                   # 22:30 -> 00:30
        bill = self.client.post(f"/api/sessions/{sid}/settle",
                                json={"ts": end}).get_json()["bill"]
        self.assertEqual(bill["total_kwh"], 120.0)
        lines = {(i["period"], i["energy_price"]): i for i in bill["breakdown"]}
        self.assertAlmostEqual(lines[("平", 0.75)]["kwh"], 30.0)
        self.assertAlmostEqual(lines[("谷", 0.30)]["kwh"], 30.0)   # 23:30 前的谷
        self.assertAlmostEqual(lines[("谷", 0.50)]["kwh"], 60.0)   # 23:30 后的谷
        # 30×1.2 + 30×0.75 + 60×0.95 = 36 + 22.5 + 57
        self.assertAlmostEqual(float(bill["total_amount"]), 115.50)

    def test_bill_frozen_and_new_session_uses_new_price(self):
        self._set_tariff(0.30, self.t0 - 3600)
        sid, end = self._charge(self.t0, 1)                   # 22:30 -> 23:30
        bill = self.client.post(f"/api/sessions/{sid}/settle",
                                json={"ts": end}).get_json()["bill"]
        self.assertAlmostEqual(float(bill["total_amount"]), 58.50)  # 36 + 30×0.75
        # 结算后调价：00:30 起谷 0.99
        self._set_tariff(0.99, ts("2026-09-11 00:30"))
        again = self.client.get(f"/api/sessions/{sid}").get_json()["bill"]
        self.assertEqual(again["total_amount"], bill["total_amount"])  # 老账单不变
        # 新充电立即用新价：01:00 -> 01:30 谷 30 度 @0.99+0.45
        sid2, end2 = self._charge(ts("2026-09-11 01:00"), 0.5)
        bill2 = self.client.post(f"/api/sessions/{sid2}/settle",
                                 json={"ts": end2}).get_json()["bill"]
        self.assertAlmostEqual(float(bill2["total_amount"]), 30 * 1.44)

    def test_invalid_periods_rejected(self):
        r = self.client.post("/api/stations/S1/tariff", json={
            "periods": [["00:00", "12:00", "谷", 0.3]], "service_fee": 0.45})
        self.assertEqual(r.status_code, 400)


class TestMeterValidation(unittest.TestCase):
    """表码上报的会话校验：不存在 / 不属于本桩 / 已终结 一律拒收。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register", json={"pile_id": "P1"})
        self.client.post("/api/piles/register", json={"pile_id": "P2"})
        self.t0 = ts("2026-09-10 22:30")
        self.sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 1000.0
        }).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": self.t0})

    def tearDown(self):
        os.unlink(self.db)

    def _send(self, pile, sid, samples):
        return self.client.post(f"/api/piles/{pile}/meter",
                                json={"session_id": sid, "samples": samples}).get_json()

    def _stored(self, sid):
        # 直接查库确认样本是否落库
        conn = server.db()
        n = conn.execute("SELECT COUNT(*) c FROM meter_samples WHERE session_id=?",
                         (sid,)).fetchone()["c"]
        conn.close()
        return n

    def test_unknown_session_rejected(self):
        r = self._send("P1", "S不存在的会话", [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}])
        self.assertEqual(r["results"][0]["status"], "rejected")
        self.assertIn("不存在", r["results"][0]["reason"])
        self.assertEqual(self._stored("S不存在的会话"), 0)   # 拒收不落库

    def test_wrong_pile_rejected(self):
        # P2 冒用 P1 的会话号上报
        r = self._send("P2", self.sid, [{"seq": 1, "ts": self.t0 + 300, "kwh": 1500.0}])
        self.assertEqual(r["results"][0]["status"], "rejected")
        self.assertIn("不属于本桩", r["results"][0]["reason"])
        self.assertEqual(self._stored(self.sid), 0)
        # 本桩正常上报不受影响
        r = self._send("P1", self.sid, [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}])
        self.assertEqual(r["results"][0]["status"], "accepted")

    def test_settled_session_rejected_but_retransmit_deduped(self):
        self._send("P1", self.sid, [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}])
        end = self.t0 + 300
        self.client.post(f"/api/sessions/{self.sid}/stop", json={"ts": end})
        self.client.post(f"/api/sessions/{self.sid}/settle", json={"ts": end})
        # 已结算：新样本拒收
        r = self._send("P1", self.sid, [{"seq": 2, "ts": self.t0 + 200, "kwh": 1003.0}])
        self.assertEqual(r["results"][0]["status"], "rejected")
        self.assertIn("已终结", r["results"][0]["reason"])
        self.assertEqual(self._stored(self.sid), 1)
        # 但结算后重发老样本仍按去重处理（幂等，不报错误拒绝）
        r = self._send("P1", self.sid, [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}])
        self.assertEqual(r["results"][0]["status"], "duplicate")

    def test_late_backfill_after_finish_still_accepted(self):
        end = self.t0 + 600
        self._send("P1", self.sid, [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0},
                                    {"seq": 2, "ts": end, "kwh": 1010.0}])
        self.client.post(f"/api/sessions/{self.sid}/stop", json={"ts": end})
        # FINISHED 状态下窗口内补报仍接受
        r = self._send("P1", self.sid, [{"seq": 3, "ts": self.t0 + 450, "kwh": 1007.5}])
        self.assertEqual(r["results"][0]["status"], "accepted")
        # 窗口外的只存档不计费
        r = self._send("P1", self.sid, [{"seq": 4, "ts": end + 300, "kwh": 1015.0}])
        self.assertEqual(r["results"][0]["status"], "late")

    def test_plugged_session_rejected(self):
        sid2 = self.client.post("/api/piles/P2/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 2000.0
        }).get_json()["session"]["session_id"]
        r = self._send("P2", sid2, [{"seq": 1, "ts": self.t0 + 300, "kwh": 2005.0}])
        self.assertEqual(r["results"][0]["status"], "rejected")
        self.assertIn("未开始充电", r["results"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
