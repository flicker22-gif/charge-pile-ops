"""端到端演示：一辆车从扫码到出账单。

场景：9月10日 22:30（平时）插枪扫码，23:00 进入谷时，60kW 充到次日 07:30（平时），
中途 02:00-03:00 桩断线（样本本地缓存，恢复后补报），
并模拟一次网络重传和一次重复结算，验证不丢电、不重复出账。

运行：python3 demo.py
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from simulator.pile_sim import Pile  # noqa: E402

PORT = 5057
SERVER = f"http://127.0.0.1:{PORT}"
POWER_KW = 60.0
STEP_MIN = 5                      # 每步模拟 5 分钟
STEP_KWH = POWER_KW * STEP_MIN / 60
REAL_SLEEP = 0.08                 # 每步真实等待，便于观看


def api(path):
    with urllib.request.urlopen(SERVER + path, timeout=5) as r:
        return json.loads(r.read())


def post(path, payload=None):
    req = urllib.request.Request(
        SERVER + path, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def log(msg):
    print(msg, flush=True)


def main():
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                api("/api/health")
                break
            except Exception:
                time.sleep(0.1)

        tariff = api("/api/tariff")
        log("=== 费率表（电费 元/kWh，服务费 %.2f）===" % tariff["service_fee"])
        for s, e, label, price in tariff["periods"]:
            log(f"  {s}-{e}  {label}  {price:.2f}")

        pile = Pile(SERVER, "CP001", "1号桩")
        pile.register()
        t = datetime(2026, 9, 10, 22, 30)   # 虚拟时钟：平时段插枪，23:00 起进入谷时

        log(f"\n=== {t:%m-%d %H:%M} 车辆到站，插枪 ===")
        r = pile.plug_in(t.timestamp())
        sid = r["session"]["session_id"]
        log(f"会话 {sid}  状态 PLUGGED  表码底数 {pile.meter_kwh:.1f} kWh")

        log(f"\n=== {t:%m-%d %H:%M} 车主扫码，启动充电 ===")
        r = post(f"/api/piles/{pile.pile_id}/scan", {"ts": t.timestamp()})
        assert r["session"]["state"] == "CHARGING"
        log(f"状态 CHARGING（重复扫码测试：{post(f'/api/piles/{pile.pile_id}/scan')['duplicated'] and '返回同一会话，幂等'}）")

        end_t = datetime(2026, 9, 11, 7, 30)
        offline_from = datetime(2026, 9, 11, 2, 0)
        offline_to = datetime(2026, 9, 11, 3, 0)
        log(f"\n=== 充电中，{POWER_KW:.0f}kW，每 {STEP_MIN} 分钟上报一次表码 ===")
        while t < end_t:
            t += timedelta(minutes=STEP_MIN)
            pile.charge(STEP_KWH)
            if t == offline_from:
                pile.disconnect()
                log(f"  {t:%H:%M}  ⚠ 桩断线，样本转本地缓存")
            pile.report_meter(t.timestamp())
            if t == offline_to:
                pile.reconnect()
                log(f"  {t:%H:%M}  ✓ 网络恢复，缓存样本已批量补报（断线 1 小时的电量未丢失）")
            if t.strftime("%H:%M") in ("23:00", "07:00"):
                log(f"  {t:%H:%M}  ⏱ 跨越费率时段边界，电量将按前后时段分别计")
            if t.minute == 0 and t.hour % 2 == 0:
                log(f"  {t:%m-%d %H:%M}  表码 {pile.meter_kwh:8.1f} kWh"
                    + ("（离线缓存中）" if not pile.online else ""))
            time.sleep(REAL_SLEEP)

        log("\n=== 模拟网络重传：最近一批样本原样重发 ===")
        r = pile.resend_last()
        log(f"服务端响应：接受 {r['accepted']} 条，去重 {r['duplicated']} 条（重发不重复计）")

        log(f"\n=== {t:%m-%d %H:%M} 车主结束充电 ===")
        r = post(f"/api/sessions/{sid}/stop", {"ts": t.timestamp()})
        log(f"状态 FINISHED  末次表码 {r['session']['end_meter']:.1f} kWh")
        pile.plug_out(t.timestamp())
        log("已拔枪")

        log("\n=== 结算 ===")
        bill = post(f"/api/sessions/{sid}/settle")["bill"]
        log(f"账单 {bill['bill_id']}  总电量 {bill['total_kwh']} kWh  总金额 ¥{bill['total_amount']}")
        log("┌────────┬────────────┬──────────┬──────────┬──────────┐")
        log("│ 时段   │ 电量(kWh)  │ 电费(元) │ 服务费   │ 小计(元) │")
        log("├────────┼────────────┼──────────┼──────────┼──────────┤")
        for it in bill["breakdown"]:
            log(f"│ {it['period']}     │ {it['kwh']:>10.1f} │ {it['energy_fee']:>8} │ "
                f"{it['service_fee']:>8} │ {it['subtotal']:>8} │")
        log("└────────┴────────────┴──────────┴──────────┴──────────┘")
        log("（谷 0.35+0.45 元/kWh，平 0.75+0.45 元/kWh —— 跨时段按实际占比分开计）")

        log("\n=== 重复结算测试（同一会话再结算一次）===")
        r2 = post(f"/api/sessions/{sid}/settle")
        same = r2["duplicated"] and r2["bill"]["bill_id"] == bill["bill_id"]
        log(f"返回账单 {r2['bill']['bill_id']}  duplicated={r2['duplicated']}  -> "
            + ("同一张账单，未重复出账 ✓" if same else "❌ 出现异常"))

        log("\n演示完成。服务端仍在运行时可访问：")
        log(f"  GET {SERVER}/api/piles            桩列表")
        log(f"  GET {SERVER}/api/sessions/{sid}  会话与账单")
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
