"""补发链测试：发送失败留档、重启恢复按序补报、重发去重、跨结束只存档、端到端结算。

三层覆盖：
- TestPileOutbox：桩本体（脚本化假传输，不依赖服务端）——先落盘再发送、
  4xx/5xx/断连保留、重启恢复、按 seq 原序、响应丢失重发去重、rejected 进死信；
- TestServerFailureInjection：服务端故障注入（下一批 meter 返回 4xx/5xx 且不落样本）；
- TestRecoveryEndToEnd：真实 HTTP + 子进程服务端，跑"失败 -> 桩进程重启 ->
  恢复补报 -> 重发去重 -> 服务端重启 -> 结算"全链。

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import app as server          # noqa: E402
from simulator.pile_sim import Pile, PileHttpError, PileOffline  # noqa: E402


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


# ---------------------------------------------------------------- 桩本体（假传输）

class FakeTransport:
    """脚本化传输层：按调用脚本返回响应或抛异常，并记录每次发出的批次。"""

    def __init__(self):
        self.script = []
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append({"path": path, "payload": payload})
        action, value = self.script.pop(0)
        if action == "ok":
            return value
        if action == "http":
            raise PileHttpError(path, value, f"mock {value}")
        if action == "offline":
            raise PileOffline()
        raise ValueError(action)


class TestPileOutbox(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="pile_state_test_")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def _make_pile(self, transport):
        p = Pile("http://mock", "CP-T1", state_dir=self.state_dir)
        p._http_post = transport
        return p

    def _plug(self, p, transport):
        transport.script.append(("ok", {"session": {"session_id": "S1"}}))
        p.plug_in(1000.0)

    def _ok_results(self, n, seq0, status="accepted"):
        return {"results": [{"seq": seq0 + i, "status": status} for i in range(n)]}

    def test_sample_persisted_before_send_and_retried_after_4xx_5xx(self):
        tr = FakeTransport()
        p = self._make_pile(tr)
        self._plug(p, tr)

        # 第一条：服务端 400；样本必须已落盘
        tr.script.append(("http", 400))
        self.assertIsNone(p.report_meter(1300.0))
        self.assertEqual(p.pending, 1)
        with open(os.path.join(self.state_dir, "CP-T1.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual([s["seq"] for s in on_disk["outbox"]], [1])

        # 第二条：服务端 503；队列里两条
        tr.script.append(("http", 503))
        self.assertIsNone(p.report_meter(1600.0))
        self.assertEqual(p.pending, 2)
        self.assertEqual(p.last_error.code, 503)

        # 断网（进程没退）：整批保留
        tr.script.append(("offline", None))
        self.assertIsNone(p.flush())
        self.assertEqual(p.pending, 2)

        # 恢复：两条一次性按 seq 顺序补发成功
        tr.script.append(("ok", self._ok_results(2, 1)))
        r = p.reconnect()
        self.assertEqual([x["status"] for x in r["results"]], ["accepted", "accepted"])
        self.assertEqual(p.pending, 0)
        # 两次失败的批量发送都带着完整队列，且顺序不变
        first_batch = tr.calls[1]["payload"]["samples"]
        self.assertEqual([s["seq"] for s in first_batch], [1])
        self.assertEqual([s["seq"] for s in tr.calls[2]["payload"]["samples"]], [1, 2])

    def test_restart_recovers_state_and_flushes_in_order(self):
        tr = FakeTransport()
        p = self._make_pile(tr)
        self._plug(p, tr)
        # seq 1 已确认；seq 2/3 发送失败（服务端 500）
        tr.script.append(("ok", self._ok_results(1, 1)))
        p.report_meter(1300.0)
        tr.script.append(("http", 500))
        p.report_meter(1600.0)
        tr.script.append(("http", 500))
        p.report_meter(1900.0)
        self.assertEqual(p.pending, 2)
        meter = p.meter_kwh

        # 桩进程重启：丢弃内存对象，同 pile_id 从状态文件恢复
        tr2 = FakeTransport()
        p2 = Pile("http://mock", "CP-T1", state_dir=self.state_dir)
        p2._http_post = tr2
        self.assertEqual(p2.session_id, "S1")
        self.assertEqual(p2.seq, 3)
        self.assertEqual(p2.meter_kwh, meter)
        self.assertEqual([s["seq"] for s in p2.outbox], [2, 3])

        # 重启后继续充电产生 seq 4：先落盘，然后整批 [2,3,4] 按序发出
        tr2.script.append(("ok", self._ok_results(3, 2)))
        p2.charge(300.0)
        r = p2.report_meter(2200.0)
        self.assertEqual([x["seq"] for x in r["results"]], [2, 3, 4])
        self.assertEqual(p2.pending, 0)

    def test_ack_lost_then_resend_is_deduped_not_double_billed(self):
        tr = FakeTransport()
        p = self._make_pile(tr)
        self._plug(p, tr)
        # 请求实际成功，但响应在回程丢了（桩只看到断连）：必须保留样本
        tr.script.append(("offline", None))
        p.report_meter(1300.0)
        self.assertEqual(p.pending, 1)
        # 重发：服务端按重复确认 duplicate，桩核销出队，不会第三次再发
        tr.script.append(("ok", self._ok_results(1, 1, status="duplicate")))
        r = p.flush()
        self.assertEqual(r["results"][0]["status"], "duplicate")
        self.assertEqual(p.pending, 0)
        # 同批之后再 flush 不会再发任何东西
        self.assertIsNone(p.flush())
        meter_calls = [c for c in tr.calls if c["path"].endswith("/meter")]
        self.assertEqual(len(meter_calls), 2)

    def test_late_sample_confirmed_and_cleared(self):
        tr = FakeTransport()
        p = self._make_pile(tr)
        self._plug(p, tr)
        tr.script.append(("ok", self._ok_results(1, 1, status="late")))
        p.report_meter(1300.0)
        self.assertEqual(p.pending, 0)          # 只存档也是服务端的最终结论，出队

    def test_rejected_goes_to_dead_letter_without_blocking_queue(self):
        tr = FakeTransport()
        p = self._make_pile(tr)
        self._plug(p, tr)
        # 离线时攒下 seq1/seq2；恢复后一批发出：seq1 永久拒收、seq2 正常
        p.disconnect()
        p.report_meter(1300.0)
        p.report_meter(1600.0)
        self.assertEqual(p.pending, 2)
        tr.script.append(("ok", {"results": [
            {"seq": 1, "status": "rejected", "reason": "会话已终结（SETTLED）"},
            {"seq": 2, "status": "accepted"}]}))
        r = p.reconnect()
        self.assertEqual([x["status"] for x in r["results"]], ["rejected", "accepted"])
        self.assertEqual(p.pending, 0)
        self.assertEqual([s["seq"] for s in p.dead_letter], [1])
        self.assertIn("SETTLED", p.dead_letter[0]["dead_reason"])
        # 死信也落盘，重启后仍可追溯、不会再发
        p2 = Pile("http://mock", "CP-T1", state_dir=self.state_dir)
        self.assertEqual([s["seq"] for s in p2.dead_letter], [1])
        self.assertEqual(p2.outbox, [])


# ---------------------------------------------------------------- 服务端故障注入

class TestServerFailureInjection(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.DB_PATH = self.db
        server.init_db()
        server._meter_failures.clear()
        self.client = server.app.test_client()
        self.client.post("/api/piles/register", json={"pile_id": "P1"})
        self.t0 = ts("2026-09-10 22:30")
        self.sid = self.client.post("/api/piles/P1/event", json={
            "type": "plug_in", "ts": self.t0, "meter_kwh": 1000.0
        }).get_json()["session"]["session_id"]
        self.client.post("/api/piles/P1/scan", json={"ts": self.t0})

    def tearDown(self):
        server._meter_failures.clear()
        os.unlink(self.db)

    def _stored(self):
        conn = server.db()
        n = conn.execute("SELECT COUNT(*) c FROM meter_samples WHERE session_id=?",
                         (self.sid,)).fetchone()["c"]
        conn.close()
        return n

    def test_injected_failures_reject_batch_and_leave_no_samples(self):
        r = self.client.post("/api/debug/meter_failures", json={"codes": [400, 503]})
        self.assertEqual(r.get_json()["queued"], [400, 503])
        sample = {"session_id": self.sid,
                  "samples": [{"seq": 1, "ts": self.t0 + 300, "kwh": 1005.0}]}
        self.assertEqual(self.client.post("/api/piles/P1/meter", json=sample).status_code, 400)
        self.assertEqual(self.client.post("/api/piles/P1/meter", json=sample).status_code, 503)
        self.assertEqual(self._stored(), 0)          # 故障期间一条都没落库
        # 故障用尽后同一条样本再发即正常受理（桩重发语义）
        r = self.client.post("/api/piles/P1/meter", json=sample).get_json()
        self.assertEqual(r["results"][0]["status"], "accepted")
        self.assertEqual(self._stored(), 1)
        # 再重发 -> duplicate，不重复计
        r = self.client.post("/api/piles/P1/meter", json=sample).get_json()
        self.assertEqual(r["results"][0]["status"], "duplicate")
        self.assertEqual(self._stored(), 1)

    def test_injection_validation(self):
        r = self.client.post("/api/debug/meter_failures", json={"code": 302})
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------- 真实 HTTP 端到端

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(method, url, payload=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class TestRecoveryEndToEnd(unittest.TestCase):
    """真实子进程服务端 + 真实 Pile：4xx 留档 -> 桩重启恢复按序补报 ->
    重发去重 -> 服务端重启不影响幂等 -> late 只存档 -> 结算电量不丢不重。"""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(prefix="charge_ops_e2e_", suffix=".db")
        os.close(self.db_fd)
        self.state_dir = tempfile.mkdtemp(prefix="pile_state_e2e_")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, CHARGE_OPS_DB=self.db_path)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(50):
            try:
                _http("GET", self.base + "/api/health")
                break
            except Exception:
                time.sleep(0.1)
        else:
            self.fail("服务端未启动")

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        shutil.rmtree(self.state_dir, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _restart_server(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        env = dict(os.environ, CHARGE_OPS_DB=self.db_path)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(50):
            try:
                _http("GET", self.base + "/api/health")
                return
            except Exception:
                time.sleep(0.1)
        self.fail("服务端重启失败")

    def test_fail_restart_recover_settle(self):
        t0 = ts("2026-09-10 22:30")
        pile = Pile(self.base, "CP-E2E", state_dir=self.state_dir)
        pile.register()
        r = pile.plug_in(t0)
        sid = r["session"]["session_id"]
        _http("POST", f"{self.base}/api/piles/CP-E2E/scan", {"ts": t0})

        # seq 1-2 正常（22:35、22:40，各 5 kWh）
        pile.charge(5.0)
        pile.report_meter(t0 + 300)
        pile.charge(5.0)
        pile.report_meter(t0 + 600)

        # 服务端连续两次报错（400、500），seq 3-4 留在桩本地
        _http("POST", self.base + "/api/debug/meter_failures", {"codes": [400, 500]})
        pile.charge(5.0)
        self.assertIsNone(pile.report_meter(t0 + 900))
        self.assertEqual(pile.last_error.code, 400)
        pile.charge(5.0)
        self.assertIsNone(pile.report_meter(t0 + 1200))
        self.assertEqual(pile.last_error.code, 500)
        self.assertEqual(pile.pending, 2)

        # 桩进程重启：新对象只靠本地状态文件恢复
        pile2 = Pile(self.base, "CP-E2E", state_dir=self.state_dir)
        self.assertEqual(pile2.session_id, sid)
        self.assertEqual(pile2.seq, 4)
        self.assertEqual([s["seq"] for s in pile2.outbox], [3, 4])
        # 恢复后 seq 3-4 按原顺序补报成功
        r = pile2.flush()
        self.assertEqual([x["seq"] for x in r["results"]], [3, 4])
        self.assertTrue(all(x["status"] == "accepted" for x in r["results"]))
        self.assertEqual(pile2.pending, 0)

        # 服务端重启（故障注入队列随进程消失，样本在 SQLite 里持久）
        self._restart_server()
        # 重启后把 seq 1-4 原样重发：全部 duplicate，绝不重复计电
        batch = [{"seq": i, "ts": t0 + i * 300, "kwh": 1000.0 + i * 5.0}
                 for i in range(1, 5)]
        r = _http("POST", f"{self.base}/api/piles/CP-E2E/meter",
                  {"session_id": sid, "samples": batch})
        self.assertTrue(all(x["status"] == "duplicate" for x in r["results"]))

        # 结束充电；结束后产生的样本只存档（late），不进账
        end = t0 + 1200
        _http("POST", f"{self.base}/api/sessions/{sid}/stop", {"ts": end})
        pile2.charge(5.0)
        r = pile2.report_meter(end + 300)
        self.assertEqual(r["results"][0]["status"], "late")

        # 结算：seq 1-4 共 20 kWh，22:30-22:40 平段，每 kWh 0.75+0.45=1.20 -> ¥24.00
        r = _http("POST", f"{self.base}/api/sessions/{sid}/settle", {"ts": end})
        bill = r["bill"]
        self.assertEqual(bill["total_kwh"], 20.0)
        self.assertAlmostEqual(float(bill["total_amount"]), 24.00)
        # 失败期间的 seq 3-4（22:45、22:50 平段各 5 kWh）确实在账上
        self.assertAlmostEqual(bill["breakdown"][0]["kwh"], 20.0)

        # 再结算一次：幂等，还是同一张账单
        r = _http("POST", f"{self.base}/api/sessions/{sid}/settle")
        self.assertTrue(r["duplicated"])
        self.assertEqual(r["bill"]["bill_id"], bill["bill_id"])


if __name__ == "__main__":
    unittest.main()
