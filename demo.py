"""端到端演示：预付费充电，余额不足自动断电，占位费并入账单。

场景：车主余额 ¥100，9月10日 22:30（平时）插枪采样，23:00 进入谷时，60kW 充电。
中途 23:40-23:50 桩断线（样本缓存补报）；23:55 起服务端连续两次表码上报报错
（400、503），样本全部留在桩本地落盘；00:05 桩进程意外重启，新进程从本地状态
文件恢复表码/seq/会话号和未确认队列，按 seq 原序补报，服务端逐条确认、重发按
duplicate 去重，电量一笔不丢也不重复计；费用逼近余额时先预警，余额不够下一间隔
时服务端下令断电，按实际充电量结算。结算后一条跨结束时间的样本按规则只存档不进
账，占位费从结算点起算，车主充值后拔枪，占位费并入同一张账单。

运行：python3 demo.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal
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
OWNER = "U1001"
# 桩本地状态目录（样本先落盘的位置）；演示用临时目录，每次跑完自动清掉
STATE_DIR = Path(tempfile.mkdtemp(prefix="pile_state_demo_"))


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
    db_fd, db_path = tempfile.mkstemp(prefix="charge_ops_demo_", suffix=".db")
    os.close(db_fd)
    server_env = dict(os.environ, CHARGE_OPS_DB=db_path)
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=server_env)
    try:
        for _ in range(50):
            try:
                api("/api/health")
                break
            except Exception:
                time.sleep(0.1)

        tariff = api("/api/tariff")
        log("=== 默认费率表（电费 元/kWh，服务费 %.2f）===" % tariff["service_fee"])
        for s, e, label, price in tariff["periods"]:
            log(f"  {s}-{e}  {label}  {price:.2f}")

        log("\n=== 场站 ST01 自定义费率（谷时 0.30 元，比默认便宜）===")
        st01_periods = [[s, e, l, (0.30 if l == "谷" else p)] for s, e, l, p in tariff["periods"]]
        post("/api/stations/ST01/tariff", {
            "periods": st01_periods, "service_fee": 0.45,
            "effective_ts": datetime(2026, 9, 10, 0, 0).timestamp()})
        log("  已发布，立即生效（POST /api/stations/ST01/tariff，不用改代码）")

        log("\n=== 运营方配置占位费规则（POST /api/config 可随时调整）===")
        post("/api/config", {"occupancy_free_minutes": 10, "occupancy_fee_per_min": 1.0})
        cfg = api("/api/config")["config"]
        log(f"  免费宽限 {cfg['occupancy_free_minutes']} 分钟，"
            f"超时 {cfg['occupancy_fee_per_min']} 元/分钟")

        log(f"\n=== 车主 {OWNER} 充值 ¥100 ===")
        r = post(f"/api/owners/{OWNER}/recharge", {"amount": "100.00"})
        log(f"账户余额 ¥{r['balance']}")

        pile = Pile(SERVER, "CP001", "1号桩", state_dir=str(STATE_DIR))
        pile.register()
        post("/api/piles/register", {"pile_id": "CP001", "name": "1号桩", "station_id": "ST01"})
        t = datetime(2026, 9, 10, 22, 30)   # 虚拟时钟：平时段插枪，23:00 起进入谷时

        log(f"\n=== {t:%m-%d %H:%M} 车辆到站，插枪 ===")
        r = pile.plug_in(t.timestamp())
        sid = r["session"]["session_id"]
        log(f"会话 {sid}  状态 PLUGGED  表码底数 {pile.meter_kwh:.1f} kWh")

        log(f"\n=== {t:%m-%d %H:%M} 车主扫码，启动充电（预付费账户 {OWNER}）===")
        r = post(f"/api/piles/{pile.pile_id}/scan", {"ts": t.timestamp(), "owner_id": OWNER})
        assert r["session"]["state"] == "CHARGING"
        log("状态 CHARGING，余额监管开启")

        end_t = datetime(2026, 9, 11, 7, 30)
        offline_from = datetime(2026, 9, 10, 23, 40)
        offline_to = datetime(2026, 9, 10, 23, 50)
        price_change_at = datetime(2026, 9, 10, 23, 30)
        fail_points = {datetime(2026, 9, 10, 23, 55), datetime(2026, 9, 11, 0, 0)}
        restart_at = datetime(2026, 9, 11, 0, 5)
        log(f"\n=== 充电中，{POWER_KW:.0f}kW，每 {STEP_MIN} 分钟上报一次表码 ===")
        cmd = None
        recovered_samples = []
        while t < end_t:
            t += timedelta(minutes=STEP_MIN)
            if t == restart_at:
                # 模拟桩进程被杀/断电后重启：新进程从本地状态文件恢复（不向服务端要任何数据）
                log(f"  {t:%H:%M}  💥 桩进程意外退出后重启：丢弃内存，只从本地状态文件恢复")
                pile = Pile(SERVER, "CP001", "1号桩", state_dir=str(STATE_DIR))
                log(f"         恢复表码 {pile.meter_kwh:.1f} kWh、seq={pile.seq}、"
                    f"会话 {pile.session_id}，未确认样本 {pile.pending} 条仍在盘上")
            pile.charge(STEP_KWH)
            if t == price_change_at:
                new_periods = [[s, e, l, (0.50 if l == "谷" else p)] for s, e, l, p in st01_periods]
                post("/api/stations/ST01/tariff", {
                    "periods": new_periods, "service_fee": 0.45,
                    "effective_ts": t.timestamp()})
                log(f"  {t:%H:%M}  💹 运营方调价：ST01 谷时 0.30 -> 0.50 元，立即生效；"
                    f"本次充电跨过调价点，前后两段各按各的价")
            if t == offline_from:
                pile.disconnect()
                log(f"  {t:%H:%M}  ⚠ 桩断线，样本转本地缓存")
            resp = pile.report_meter(t.timestamp())
            if t == offline_to:
                resp = pile.reconnect()
                # 从下一批表码开始制造两次服务端故障（400、503），样本一条都不能丢
                post("/api/debug/meter_failures", {"codes": [400, 503]})
                s = resp["summary"]
                log(f"  {t:%H:%M}  ✓ 网络恢复，缓存样本批量补报 -> "
                    f"接受 {s['accepted']} 条，重复 {s['duplicate']} 条，晚于结束 {s['late']} 条")
            if t in fail_points and resp is None:
                code = getattr(pile.last_error, "code", "断连")
                log(f"  {t:%H:%M}  ❌ 服务端返回 {code}，本批没拿到确认 -> "
                    f"样本已在本地落盘，补发队列 {pile.pending} 条，恢复后按 seq 原序重发")
            if t == restart_at:
                s = resp["summary"]
                recovered_samples = list(pile.last_sent)   # 补报批用于稍后演示重传
                log(f"  {t:%H:%M}  ♻ 重启后自动补报 -> "
                    f"接受 {s['accepted']} 条，重复 {s['duplicate']} 条，晚于结束 {s['late']} 条，"
                    f"seq {[x['seq'] for x in recovered_samples]} 按原顺序到达，"
                    f"本地未确认队列剩余 {pile.pending} 条")
            if resp and resp.get("warning"):
                log(f"  {t:%H:%M}  ⚠ 服务端预警 -> {resp['warning']}")
            if t.strftime("%H:%M") in ("23:00", "07:00"):
                log(f"  {t:%H:%M}  ⏱ 跨越费率时段边界，电量将按前后时段分别计")
            if t.minute == 0 and not resp:
                log(f"  {t:%m-%d %H:%M}  表码 {pile.meter_kwh:8.1f} kWh（离线/失败，样本本地留存中）")
            elif t.minute == 0:
                log(f"  {t:%m-%d %H:%M}  表码 {pile.meter_kwh:8.1f} kWh")
            if resp and resp.get("cmd"):
                cmd = resp["cmd"]
                log(f"  {t:%m-%d %H:%M}  ⛔ 桩收到断电指令并执行 -> {cmd['message']}")
                break
            time.sleep(REAL_SLEEP)

        log("\n=== 模拟网络重传：把重启后补报过的那批样本原样再发一遍 ===")
        r = post(f"/api/piles/{pile.pile_id}/meter",
                 {"session_id": sid, "samples": recovered_samples})
        s = r["summary"]
        log(f"服务端分类：接受 {s['accepted']}，重复 {s['duplicate']}，晚于结束 {s['late']}"
            f"（seq {r['results'][0]['seq']} -> {r['results'][0]['status']}，"
            f"服务端早收过，重发不重复计电）")

        log(f"\n=== {t:%m-%d %H:%M} 充电结束（原因：{cmd['reason'] if cmd else 'user'}）===")
        r = post(f"/api/sessions/{sid}/stop",
                 {"ts": t.timestamp(), "reason": cmd["reason"] if cmd else "user"})
        log(f"状态 FINISHED  末次表码 {r['session']['end_meter']:.1f} kWh")

        log("\n=== 结束后又晚到一条表码（+5 kWh，模拟断线桩的滞后报文）===")
        pile.charge(STEP_KWH)
        resp = pile.report_meter((t + timedelta(minutes=STEP_MIN)).timestamp())
        r0 = resp["results"][0]
        log(f"服务端分类：{r0['status']} -> {r0['reason']}")

        log("\n=== 窗口内的晚到补报仍被接受（结束时间之前产生，只是晚到）===")
        resp = post(f"/api/piles/{pile.pile_id}/meter", {"session_id": sid, "samples": [
            {"seq": 90, "ts": (t - timedelta(minutes=STEP_MIN)).timestamp(),
             "kwh": pile.meter_kwh - 2 * STEP_KWH}]})
        r0 = resp["results"][0]
        log(f"服务端分类：{r0['status']}（补报通道不受校验影响）")

        log("\n=== 结算（按实际充电量，从余额扣款）===")
        r = post(f"/api/sessions/{sid}/settle", {"ts": t.timestamp()})
        bill = r["bill"]
        log(f"账单 {bill['bill_id']}  总电量 {bill['total_kwh']} kWh  充电费 ¥{bill['total_amount']}")
        log("┌────────┬───────────┬────────────┬──────────┬──────────┬──────────┐")
        log("│ 时段   │ 单价(元)  │ 电量(kWh)  │ 电费(元) │ 服务费   │ 小计(元) │")
        log("├────────┼───────────┼────────────┼──────────┼──────────┼──────────┤")
        for it in bill["breakdown"]:
            log(f"│ {it['period']}     │ {it['energy_price']:.2f}+{it['service_price']:.2f} │ "
                f"{it['kwh']:>10.1f} │ {it['energy_fee']:>8} │ {it['service_fee']:>8} │ {it['subtotal']:>8} │")
        log("└────────┴───────────┴────────────┴──────────┴──────────┴──────────┘")
        log("（谷时段被调价点切成两行：23:30 前 0.30+0.45，23:30 后 0.50+0.45，各算各的）")
        log(f"扣款后账户余额 ¥{r.get('owner_balance')}（结束后晚到的 5 kWh 未计入）")

        log("\n=== 账单冻结验证：结算后再调价，老账单不变 ===")
        frozen = bill["total_amount"]
        post("/api/stations/ST01/tariff", {
            "periods": [[s, e, l, (0.99 if l == "谷" else p)] for s, e, l, p in st01_periods],
            "service_fee": 0.45, "effective_ts": datetime(2026, 9, 12, 0, 0).timestamp()})
        again = api(f"/api/sessions/{sid}")["bill"]["total_amount"]
        log(f"  谷时又调到 0.99 元（次日生效），本单金额仍为 ¥{again}"
            + (" ✓" if again == frozen else " ❌ 被调价影响！"))

        log("\n=== 重复结算测试（同一会话再结算一次）===")
        r2 = post(f"/api/sessions/{sid}/settle")
        same = r2["duplicated"] and r2["bill"]["bill_id"] == bill["bill_id"]
        log(f"返回账单 {r2['bill']['bill_id']}  duplicated={r2['duplicated']}  -> "
            + ("同一张账单，未重复出账 ✓" if same else "❌ 出现异常"))

        log("\n=== 安全校验：冒用/过期会话号的上报被拒收 ===")
        post("/api/piles/register", {"pile_id": "CP002", "name": "2号桩", "station_id": "ST01"})
        fake = [{"seq": 999, "ts": t.timestamp(), "kwh": 9999.0}]
        r = post("/api/piles/CP002/meter", {"session_id": sid, "samples": fake})
        log(f"  2号桩冒用本会话号上报   -> {r['results'][0]['status']}: {r['results'][0]['reason']}")
        r = post(f"/api/piles/{pile.pile_id}/meter", {"session_id": "S伪造会话", "samples": fake})
        log(f"  编造不存在的会话号上报  -> {r['results'][0]['status']}: {r['results'][0]['reason']}")
        r = post(f"/api/piles/{pile.pile_id}/meter", {"session_id": sid, "samples": [
            {"seq": 91, "ts": (t - timedelta(minutes=3)).timestamp(), "kwh": 1098.0}]})
        log(f"  已结算会话再收到新样本  -> {r['results'][0]['status']}: {r['results'][0]['reason']}")
        r = post(f"/api/piles/{pile.pile_id}/meter", {"session_id": sid, "samples": [
            {"seq": 90, "ts": (t - timedelta(minutes=STEP_MIN)).timestamp(),
             "kwh": pile.meter_kwh - 2 * STEP_KWH}]})
        log(f"  已结算后重发老样本      -> {r['results'][0]['status']}（幂等去重，不误拒）")

        log("\n=== 车主充完没拔枪，占位计时开始 ===")
        t8 = (t + timedelta(minutes=8)).timestamp()
        occ = api(f"/api/sessions/{sid}?ts={t8}")["occupancy_running"]
        log(f"  结算后  8 分钟：占位费 ¥{occ['fee']}（免费宽限 {occ['free_minutes']} 分钟内）")
        t25 = (t + timedelta(minutes=25)).timestamp()
        occ = api(f"/api/sessions/{sid}?ts={t25}")["occupancy_running"]
        log(f"  结算后 25 分钟：占位费 ¥{occ['fee']} 在跑"
            f"（超宽限 {occ['billable_minutes']} 分钟 × {occ['fee_per_min']} 元）")

        log("\n=== 车主看到占位费在跑，充值 ¥50 后回来拔枪 ===")
        r = post(f"/api/owners/{OWNER}/recharge", {"amount": "50.00"})
        log(f"充值后余额 ¥{r['balance']}")
        r = pile.plug_out(t25)
        bill = r["bill"]
        occ = bill["occupancy"]
        log(f"已拔枪，会话 CLOSED。占位 {occ['elapsed_minutes']} 分钟"
            f"（宽限 {occ['free_minutes']}），占位费 ¥{occ['fee']} 并入本单")

        log("\n=== 最终账单 ===")
        charging_fee = Decimal(bill["total_amount"]) - Decimal(occ["fee"])
        log(f"  充电费 ¥{charging_fee}（{bill['total_kwh']} kWh）"
            f" + 占位费 ¥{occ['fee']}（{occ['billable_minutes']} 分钟）"
            f" = 合计 ¥{bill['total_amount']}")

        log("\n=== 车主账户 ===")
        owner = api(f"/api/owners/{OWNER}")
        log(f"余额 ¥{owner['balance']}")
        for n in owner["notifications"]:
            log(f"  通知[{n['type']}] {n['message']}")

        log("\n演示完成。服务端仍在运行时可访问：")
        log(f"  GET {SERVER}/api/piles            桩列表")
        log(f"  GET {SERVER}/api/sessions/{sid}  会话与账单")
    finally:
        server.terminate()
        server.wait(timeout=5)
        shutil.rmtree(STATE_DIR, ignore_errors=True)   # 清桩本地状态目录
        for suffix in ("", "-wal", "-shm"):           # 清临时数据库
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
