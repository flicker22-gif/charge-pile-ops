"""充电站运营后端：桩接入、扫码充电流程、峰谷分时计费、预付费断电、占位费、幂等结算。

运行：python3 server/app.py [--port 5000]
数据：SQLite，文件在本目录 charge_ops.db
"""
import argparse
import json
import math
import os
import sqlite3
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP

from flask import Flask, jsonify, request

from tariff import PERIODS, SERVICE_FEE, period_at, price_of, split_by_period

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charge_ops.db")

# 会话状态机：
# PLUGGED(插枪) -> CHARGING(充电中) -> FINISHED(结束) -> SETTLED(已结算) -> CLOSED(拔枪离场)
# 占位计时从 SETTLED 开始，到 CLOSED 截止，占位费并入同一张账单。
SCHEMA = """
CREATE TABLE IF NOT EXISTS piles (
    pile_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'offline',
    meter_kwh   REAL NOT NULL DEFAULT 0,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS owners (
    owner_id    TEXT PRIMARY KEY,
    balance_fen INTEGER NOT NULL DEFAULT 0   -- 余额，单位分，避免浮点误差
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    pile_id     TEXT NOT NULL,
    owner_id    TEXT,
    state       TEXT NOT NULL,
    plug_ts     REAL,
    start_ts    REAL,
    end_ts      REAL,
    start_meter REAL,
    end_meter   REAL,
    stop_reason TEXT,
    occupancy_start_ts REAL,
    plug_out_ts REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_pile_state ON sessions(pile_id, state);
CREATE TABLE IF NOT EXISTS meter_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         REAL NOT NULL,
    kwh        REAL NOT NULL,
    UNIQUE(session_id, seq)          -- 断线补报/重发同一条样本不会产生重复
);
CREATE TABLE IF NOT EXISTS bills (
    bill_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL UNIQUE, -- 一次充电只有一张账单，重复结算返回同一张
    total_kwh   REAL NOT NULL,
    total_amount TEXT NOT NULL,       -- 充电费 + 占位费（拔枪后定格）
    breakdown   TEXT NOT NULL,        -- 充电费分时明细
    occupancy   TEXT,                 -- 占位费明细，拔枪时写入
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    session_id TEXT,
    type       TEXT NOT NULL,         -- balance_warning / balance_stop
    message    TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
);
"""

# 运营方可通过 POST /api/config 调整
DEFAULT_CONFIG = {
    "occupancy_free_minutes": "15",   # 结算后免费占位宽限（分钟）
    "occupancy_fee_per_min": "0.5",   # 超过宽限后的占位费（元/分钟）
    "low_balance_warn_ratio": "0.7",  # 已产生费用占余额比例达到该值时预警
}

app = Flask(__name__)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_column(conn, table, col, ddl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    # 兼容旧库文件：补新列
    for col, ddl in [("owner_id", "TEXT"), ("stop_reason", "TEXT"),
                     ("occupancy_start_ts", "REAL"), ("plug_out_ts", "REAL")]:
        _ensure_column(conn, "sessions", col, ddl)
    _ensure_column(conn, "bills", "occupancy", "TEXT")
    for k, v in DEFAULT_CONFIG.items():
        conn.execute("INSERT OR IGNORE INTO config(key, value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()


def now():
    return time.time()


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def row_dict(r):
    return {k: r[k] for k in r.keys()}


def yuan_to_fen(v):
    return int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fen_to_yuan(fen):
    return str((Decimal(fen) / 100).quantize(Decimal("0.01")))


def get_cfg(conn, key):
    r = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULT_CONFIG[key]


def notify_once(conn, owner_id, sid, ntype, message, ts):
    """同一会话同一类通知只发一次。"""
    conn.execute(
        "INSERT INTO notifications(owner_id, session_id, type, message, ts)"
        " SELECT ?,?,?,?,? WHERE NOT EXISTS"
        " (SELECT 1 FROM notifications WHERE session_id=? AND type=?)",
        (owner_id, sid, ntype, message, ts, sid, ntype))


def deduct(conn, owner_id, amount_yuan):
    """从余额扣款（金额字符串，元）。允许为 0。"""
    fen = yuan_to_fen(amount_yuan)
    if fen:
        conn.execute("UPDATE owners SET balance_fen = balance_fen - ? WHERE owner_id=?",
                     (fen, owner_id))


def get_active_session(conn, pile_id):
    return conn.execute(
        "SELECT * FROM sessions WHERE pile_id=? AND state IN ('PLUGGED','CHARGING')"
        " ORDER BY created_at DESC LIMIT 1",
        (pile_id,),
    ).fetchone()


# ---------------- 车主账户 ----------------

@app.post("/api/owners/<owner_id>/recharge")
def recharge(owner_id):
    """充值（也用于开户）：{amount: "100.00"} 元。"""
    body = request.get_json(force=True)
    amount = body.get("amount")
    if amount is None or yuan_to_fen(amount) <= 0:
        return err("amount 必须为正数（元）")
    fen = yuan_to_fen(amount)
    conn = db()
    conn.execute(
        "INSERT INTO owners(owner_id, balance_fen) VALUES(?,?) "
        "ON CONFLICT(owner_id) DO UPDATE SET balance_fen = balance_fen + excluded.balance_fen",
        (owner_id, fen))
    conn.commit()
    bal = conn.execute("SELECT balance_fen FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "owner_id": owner_id, "balance": fen_to_yuan(bal["balance_fen"])})


@app.get("/api/owners/<owner_id>")
def owner_detail(owner_id):
    conn = db()
    owner = conn.execute("SELECT * FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    if not owner:
        conn.close()
        return err("车主账户不存在", 404)
    notifs = [row_dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE owner_id=? ORDER BY id", (owner_id,))]
    conn.close()
    return jsonify({"ok": True, "owner_id": owner_id,
                    "balance": fen_to_yuan(owner["balance_fen"]), "notifications": notifs})


@app.route("/api/config", methods=["GET", "POST"])
def config():
    conn = db()
    if request.method == "POST":
        body = request.get_json(force=True)
        for k in body:
            if k not in DEFAULT_CONFIG:
                conn.close()
                return err(f"未知配置项: {k}")
        for k, v in body.items():
            conn.execute("INSERT INTO config(key, value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        conn.commit()
    cfg = {k: get_cfg(conn, k) for k in DEFAULT_CONFIG}
    conn.close()
    return jsonify({"ok": True, "config": cfg})


# ---------------- 桩接入 ----------------

@app.post("/api/piles/register")
def register_pile():
    body = request.get_json(force=True)
    pile_id = body.get("pile_id")
    if not pile_id:
        return err("pile_id required")
    conn = db()
    conn.execute(
        "INSERT INTO piles(pile_id, name, status, last_seen) VALUES(?,?, 'online', ?) "
        "ON CONFLICT(pile_id) DO UPDATE SET name=excluded.name, status='online', last_seen=excluded.last_seen",
        (pile_id, body.get("name", ""), now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "pile_id": pile_id})


@app.post("/api/piles/<pile_id>/event")
def pile_event(pile_id):
    """桩事件：plug_in 插枪 / plug_out 拔枪。"""
    body = request.get_json(force=True)
    etype = body.get("type")
    ts = float(body.get("ts") or now())
    meter = body.get("meter_kwh")
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE piles SET status='online', last_seen=?, meter_kwh=COALESCE(?, meter_kwh) WHERE pile_id=?",
                 (now(), meter, pile_id))

    if etype == "plug_in":
        sess = get_active_session(conn, pile_id)
        if sess:
            conn.commit()
            conn.close()
            # 幂等：重复上报插枪返回已存在的会话
            return jsonify({"ok": True, "session": row_dict(sess), "duplicated": True})
        sid = "S" + uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO sessions(session_id, pile_id, state, plug_ts, start_meter, created_at)"
            " VALUES(?,?, 'PLUGGED', ?, ?, ?)",
            (sid, pile_id, ts, meter, now()),
        )
        conn.commit()
        sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "session": row_dict(sess)})

    if etype == "plug_out":
        # 拔枪才真正结束：未结算的先自动结算，再计占位费并入账单，会话关闭
        sess = conn.execute(
            "SELECT * FROM sessions WHERE pile_id=?"
            " AND state IN ('PLUGGED','CHARGING','FINISHED','SETTLED','CLOSED')"
            " ORDER BY created_at DESC LIMIT 1", (pile_id,)).fetchone()
        if not sess or sess["state"] == "CLOSED":
            conn.commit()
            conn.close()
            return jsonify({"ok": True, "duplicated": True})
        if sess["state"] == "CHARGING":
            conn.rollback()
            conn.close()
            return err("充电中不能拔枪，请先结束充电", 409)
        if sess["state"] == "PLUGGED":
            conn.execute("UPDATE sessions SET state='CANCELLED', plug_out_ts=? WHERE session_id=?",
                         (ts, sess["session_id"]))
            conn.commit()
            conn.close()
            return jsonify({"ok": True, "cancelled": True})
        if sess["state"] == "FINISHED":
            _settle(conn, sess, ts)
            sess = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                                (sess["session_id"],)).fetchone()
        # SETTLED：计占位费，并入账单
        occ = compute_occupancy(conn, sess, ts)
        bill = conn.execute("SELECT * FROM bills WHERE session_id=?",
                            (sess["session_id"],)).fetchone()
        total = str(Decimal(bill["total_amount"]) + Decimal(occ["fee"]))
        conn.execute("UPDATE bills SET occupancy=?, total_amount=? WHERE session_id=?",
                     (json.dumps(occ, ensure_ascii=False), total, sess["session_id"]))
        if sess["owner_id"]:
            deduct(conn, sess["owner_id"], occ["fee"])
        conn.execute("UPDATE sessions SET state='CLOSED', plug_out_ts=? WHERE session_id=?",
                     (ts, sess["session_id"]))
        conn.commit()
        bill = conn.execute("SELECT * FROM bills WHERE session_id=?",
                            (sess["session_id"],)).fetchone()
        sess = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                            (sess["session_id"],)).fetchone()
        conn.close()
        return jsonify({"ok": True, "session": row_dict(sess), "bill": _bill_dict(bill)})

    conn.close()
    return err(f"unknown event type: {etype}")


@app.post("/api/piles/<pile_id>/scan")
def scan(pile_id):
    """车主扫码启动充电：PLUGGED -> CHARGING。重复扫码返回同一会话（幂等）。

    body 可带 owner_id：预付费账户，余额不足拒绝启动；不带则按访客充电。
    """
    body = request.get_json(force=True, silent=True) or {}
    ts = float(body.get("ts") or now())
    owner_id = body.get("owner_id")
    conn = db()
    if owner_id:
        owner = conn.execute("SELECT * FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
        if not owner:
            conn.close()
            return err("车主账户不存在，请先充值开户", 404)
        if owner["balance_fen"] <= 0:
            conn.close()
            return err("余额不足，请先充值", 402)
    sess = get_active_session(conn, pile_id)
    if not sess:
        conn.close()
        return err("未插枪，无法启动充电", 409)
    if sess["state"] == "CHARGING":
        conn.close()
        return jsonify({"ok": True, "session": row_dict(sess), "duplicated": True})
    conn.execute("UPDATE sessions SET state='CHARGING', start_ts=?, owner_id=?"
                 " WHERE session_id=? AND state='PLUGGED'",
                 (ts, owner_id, sess["session_id"]))
    conn.commit()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
    conn.close()
    return jsonify({"ok": True, "session": row_dict(sess)})


@app.post("/api/piles/<pile_id>/meter")
def meter_report(pile_id):
    """电表上报：{session_id, samples:[{seq, ts, kwh}]}。

    kwh 是电表累计读数。桩断线时本地缓存、恢复后批量补报；
    UNIQUE(session_id, seq) 保证补报/重发不会重复计入。
    响应里带余额监管指令：warning 预警 / cmd 断电指令，桩侧须执行并提示车主。
    """
    body = request.get_json(force=True)
    sid = body.get("session_id")
    samples = body.get("samples") or []
    conn = db()
    conn.execute("UPDATE piles SET last_seen=?, status='online' WHERE pile_id=?", (now(), pile_id))
    accepted = 0
    for s in samples:
        cur = conn.execute(
            "INSERT OR IGNORE INTO meter_samples(session_id, seq, ts, kwh) VALUES(?,?,?,?)",
            (sid, int(s["seq"]), float(s["ts"]), float(s["kwh"])),
        )
        accepted += cur.rowcount
    if samples:
        latest = max(float(s["kwh"]) for s in samples)
        conn.execute("UPDATE piles SET meter_kwh=MAX(meter_kwh, ?) WHERE pile_id=?", (latest, pile_id))

    # ---- 余额监管：已产生费用 + 下一间隔预估 >= 余额 则下令断电 ----
    warning, cmd = None, None
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if sess and sess["state"] == "CHARGING" and sess["owner_id"]:
        owner = conn.execute("SELECT * FROM owners WHERE owner_id=?",
                             (sess["owner_id"],)).fetchone()
        all_samples = [row_dict(r) for r in conn.execute(
            "SELECT seq, ts, kwh FROM meter_samples WHERE session_id=? ORDER BY ts, seq", (sid,))]
        if owner and len(all_samples) >= 2:
            _, accrued, _, _ = compute_bill(sess, all_samples)
            accrued_d = Decimal(accrued)
            balance_d = Decimal(owner["balance_fen"]) / 100
            last, prev = all_samples[-1], all_samples[-2]
            price = Decimal(str(price_of(period_at(last["ts"])) + SERVICE_FEE))
            est_next = Decimal(f"{last['kwh'] - prev['kwh']:.6f}") * price
            warn_ratio = Decimal(get_cfg(conn, "low_balance_warn_ratio"))
            if accrued_d + est_next >= balance_d:
                msg = (f"余额不足：已产生费用 ¥{accrued}，账户余额 ¥{balance_d}，"
                       f"已断电结束充电，将按实际充电量结算")
                notify_once(conn, sess["owner_id"], sid, "balance_stop", msg, last["ts"])
                cmd = {"action": "STOP", "reason": "balance_insufficient", "message": msg}
            elif accrued_d >= balance_d * warn_ratio:
                msg = (f"余额预警：已产生费用 ¥{accrued}，账户余额 ¥{balance_d}，"
                       f"余额不足时将自动断电，请及时充值或结束充电")
                notify_once(conn, sess["owner_id"], sid, "balance_warning", msg, last["ts"])
                warning = msg
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "accepted": accepted,
                    "duplicated": len(samples) - accepted,
                    "warning": warning, "cmd": cmd})


@app.post("/api/piles/<pile_id>/heartbeat")
def heartbeat(pile_id):
    body = request.get_json(force=True, silent=True) or {}
    conn = db()
    conn.execute("UPDATE piles SET status=?, last_seen=? WHERE pile_id=?",
                 (body.get("status", "online"), now(), pile_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- 充电流程 ----------------

@app.post("/api/sessions/<sid>/stop")
def stop(sid):
    """结束充电：CHARGING -> FINISHED，定格末次表码。reason: user / balance_insufficient。"""
    body = request.get_json(force=True, silent=True) or {}
    ts = float(body.get("ts") or now())
    reason = body.get("reason", "user")
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not sess:
        conn.close()
        return err("会话不存在", 404)
    if sess["state"] != "CHARGING":
        conn.close()
        return err(f"当前状态 {sess['state']} 不能结束", 409)
    last = conn.execute(
        "SELECT kwh FROM meter_samples WHERE session_id=? AND ts<=?"
        " ORDER BY ts DESC, seq DESC LIMIT 1", (sid, ts)).fetchone()
    end_meter = last["kwh"] if last else sess["start_meter"]
    conn.execute("UPDATE sessions SET state='FINISHED', end_ts=?, end_meter=?, stop_reason=?"
                 " WHERE session_id=?", (ts, end_meter, reason, sid))
    conn.commit()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "session": row_dict(sess)})


def compute_bill(sess, samples):
    """按表码样本把电量切到各费率时段：每段样本区间的电量按时间占比分摊。

    只计入 ts <= end_ts 的样本：结束前产生、只是晚到的补报照常算；
    结束之后才产生的样本不影响本次结算。
    """
    end_ts = sess["end_ts"]
    if end_ts is not None:
        samples = [s for s in samples if s["ts"] <= end_ts]
    points = [(sess["start_ts"], sess["start_meter"])] + [(s["ts"], s["kwh"]) for s in samples]
    points.sort(key=lambda p: p[0])
    kwh_by_label = {}
    for (t0, e0), (t1, e1) in zip(points, points[1:]):
        delta = e1 - e0
        if delta <= 0 or t1 <= t0:
            continue
        for label, secs in split_by_period(t0, t1).items():
            kwh_by_label[label] = kwh_by_label.get(label, 0.0) + delta * secs / (t1 - t0)

    total_kwh = points[-1][1] - points[0][1]
    items, total = [], Decimal("0")
    for label in ("峰", "平", "谷"):
        kwh = kwh_by_label.get(label, 0.0)
        if kwh <= 0:
            continue
        energy_fee = (Decimal(f"{kwh:.6f}") * Decimal(str(price_of(label)))).quantize(
            Decimal("0.01"), ROUND_HALF_UP)
        service_fee = (Decimal(f"{kwh:.6f}") * Decimal(str(SERVICE_FEE))).quantize(
            Decimal("0.01"), ROUND_HALF_UP)
        items.append({
            "period": label,
            "kwh": round(kwh, 3),
            "energy_price": price_of(label),
            "service_price": SERVICE_FEE,
            "energy_fee": str(energy_fee),
            "service_fee": str(service_fee),
            "subtotal": str(energy_fee + service_fee),
        })
        total += energy_fee + service_fee
    return round(total_kwh, 3), str(total), items, points[-1][1]


def compute_occupancy(conn, sess, ts):
    """占位费：从结算时刻起计时，超出免费宽限的部分按分钟计费。"""
    free_min = int(get_cfg(conn, "occupancy_free_minutes"))
    rate = Decimal(get_cfg(conn, "occupancy_fee_per_min"))
    start = sess["occupancy_start_ts"]
    elapsed_min = max(0, math.ceil((ts - start) / 60)) if start else 0
    billable = max(0, elapsed_min - free_min)
    fee = (Decimal(billable) * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {
        "start_ts": start, "end_ts": ts,
        "elapsed_minutes": elapsed_min,
        "free_minutes": free_min,
        "billable_minutes": billable,
        "fee_per_min": str(rate),
        "fee": str(fee),
    }


def _settle(conn, sess, ts):
    """在已有事务内结算：出充电账单、扣余额、开始占位计时。返回 (bill_row, duplicated)。"""
    sid = sess["session_id"]
    existing = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
    if existing:
        return existing, True
    samples = [row_dict(r) for r in conn.execute(
        "SELECT seq, ts, kwh FROM meter_samples WHERE session_id=? ORDER BY ts, seq", (sid,))]
    total_kwh, total_amount, items, final_meter = compute_bill(sess, samples)
    # 晚到的窗口内补报可能把末次表码推高，结算时按窗口内样本重新定格
    bill_id = "B" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO bills(bill_id, session_id, total_kwh, total_amount, breakdown, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (bill_id, sid, total_kwh, total_amount, json.dumps(items, ensure_ascii=False), now()))
    conn.execute("UPDATE sessions SET state='SETTLED', end_meter=?, occupancy_start_ts=?"
                 " WHERE session_id=?", (final_meter, ts, sid))
    if sess["owner_id"]:
        deduct(conn, sess["owner_id"], total_amount)
    return conn.execute("SELECT * FROM bills WHERE bill_id=?", (bill_id,)).fetchone(), False


@app.post("/api/sessions/<sid>/settle")
def settle(sid):
    """结算：一次充电只出一张账单。重复调用返回已生成的账单（幂等）。

    结算后从车主余额扣充电费，并开始占位计时（拔枪才停止）。
    """
    body = request.get_json(force=True, silent=True) or {}
    ts = float(body.get("ts") or now())
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not sess:
        conn.close()
        return err("会话不存在", 404)
    existing = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
    if existing:
        conn.commit()
        out = {"ok": True, "bill": _bill_dict(existing), "duplicated": True}
        conn.close()
        return jsonify(out)
    if sess["state"] not in ("FINISHED", "SETTLED"):
        conn.rollback()
        conn.close()
        return err(f"当前状态 {sess['state']} 不能结算，请先结束充电", 409)
    try:
        bill, _ = _settle(conn, sess, ts)
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发下另一请求已结算：回滚后返回那张账单，绝不重复出账
        conn.rollback()
        existing = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "bill": _bill_dict(existing), "duplicated": True})
    out = {"ok": True, "bill": _bill_dict(bill)}
    if sess["owner_id"]:
        bal = conn.execute("SELECT balance_fen FROM owners WHERE owner_id=?",
                           (sess["owner_id"],)).fetchone()
        out["owner_balance"] = fen_to_yuan(bal["balance_fen"])
    conn.close()
    return jsonify(out)


def _bill_dict(row):
    d = row_dict(row)
    d["breakdown"] = json.loads(d["breakdown"])
    d["occupancy"] = json.loads(d["occupancy"]) if d.get("occupancy") else None
    return d


# ---------------- 查询 ----------------

@app.get("/api/piles")
def list_piles():
    conn = db()
    rows = [row_dict(r) for r in conn.execute("SELECT * FROM piles ORDER BY pile_id")]
    conn.close()
    return jsonify({"ok": True, "piles": rows})


@app.get("/api/sessions/<sid>")
def get_session(sid):
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not sess:
        conn.close()
        return err("会话不存在", 404)
    bill = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
    out = {"ok": True, "session": row_dict(sess)}
    if bill:
        out["bill"] = _bill_dict(bill)
    # 已结算未拔枪：返回实时占位费（车主端可看到费用在跑），?ts= 可指定时刻
    if sess["state"] == "SETTLED":
        ts = float(request.args.get("ts") or now())
        out["occupancy_running"] = compute_occupancy(conn, sess, ts)
    conn.close()
    return jsonify(out)


@app.get("/api/tariff")
def get_tariff():
    return jsonify({"ok": True, "periods": PERIODS, "service_fee": SERVICE_FEE})


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    init_db()
    app.run(host="127.0.0.1", port=args.port)
