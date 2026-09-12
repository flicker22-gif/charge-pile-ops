"""充电站运营后端：桩接入、扫码充电流程、峰谷分时计费、幂等结算。

运行：python3 server/app.py [--port 5000]
数据：SQLite，文件在本目录 charge_ops.db
"""
import argparse
import json
import os
import sqlite3
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP

from flask import Flask, jsonify, request

from tariff import PERIODS, SERVICE_FEE, price_of, split_by_period

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charge_ops.db")

# 会话状态机：PLUGGED(插枪) -> CHARGING(充电中) -> FINISHED(结束) -> SETTLED(已结算)
SCHEMA = """
CREATE TABLE IF NOT EXISTS piles (
    pile_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'offline',
    meter_kwh   REAL NOT NULL DEFAULT 0,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    pile_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    plug_ts     REAL,
    start_ts    REAL,
    end_ts      REAL,
    start_meter REAL,
    end_meter   REAL,
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
    total_amount TEXT NOT NULL,
    breakdown   TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""

app = Flask(__name__)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now():
    return time.time()


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def row_dict(r):
    return {k: r[k] for k in r.keys()}


def get_active_session(conn, pile_id):
    return conn.execute(
        "SELECT * FROM sessions WHERE pile_id=? AND state IN ('PLUGGED','CHARGING')"
        " ORDER BY created_at DESC LIMIT 1",
        (pile_id,),
    ).fetchone()


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
    """桩事件：plug_in 插枪 / plug_out 拔枪。插枪即建会话并记录电表底数。"""
    body = request.get_json(force=True)
    etype = body.get("type")
    ts = float(body.get("ts") or now())
    meter = body.get("meter_kwh")
    conn = db()
    conn.execute("UPDATE piles SET status='online', last_seen=?, meter_kwh=COALESCE(?, meter_kwh) WHERE pile_id=?",
                 (now(), meter, pile_id))
    sess = get_active_session(conn, pile_id)

    if etype == "plug_in":
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
        if sess and sess["state"] == "CHARGING":
            conn.close()
            return err("充电中不能拔枪，请先结束充电", 409)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    conn.close()
    return err(f"unknown event type: {etype}")


@app.post("/api/piles/<pile_id>/scan")
def scan(pile_id):
    """车主扫码启动充电：PLUGGED -> CHARGING。重复扫码返回同一会话（幂等）。"""
    body = request.get_json(force=True, silent=True) or {}
    ts = float(body.get("ts") or now())
    conn = db()
    sess = get_active_session(conn, pile_id)
    if not sess:
        conn.close()
        return err("未插枪，无法启动充电", 409)
    if sess["state"] == "CHARGING":
        conn.close()
        return jsonify({"ok": True, "session": row_dict(sess), "duplicated": True})
    conn.execute("UPDATE sessions SET state='CHARGING', start_ts=? WHERE session_id=? AND state='PLUGGED'",
                 (ts, sess["session_id"]))
    conn.commit()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
    conn.close()
    return jsonify({"ok": True, "session": row_dict(sess)})


@app.post("/api/piles/<pile_id>/meter")
def meter_report(pile_id):
    """电表上报：{session_id, samples:[{seq, ts, kwh}]}。

    kwh 是电表累计读数。桩断线时本地缓存、恢复后批量补报；
    UNIQUE(session_id, seq) 保证补报/重发不会重复计入。
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
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "accepted": accepted, "duplicated": len(samples) - accepted})


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
    """结束充电：CHARGING -> FINISHED，定格末次表码。"""
    body = request.get_json(force=True, silent=True) or {}
    ts = float(body.get("ts") or now())
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not sess:
        conn.close()
        return err("会话不存在", 404)
    if sess["state"] != "CHARGING":
        conn.close()
        return err(f"当前状态 {sess['state']} 不能结束", 409)
    last = conn.execute(
        "SELECT kwh FROM meter_samples WHERE session_id=? ORDER BY ts DESC, seq DESC LIMIT 1", (sid,)
    ).fetchone()
    end_meter = last["kwh"] if last else sess["start_meter"]
    conn.execute("UPDATE sessions SET state='FINISHED', end_ts=?, end_meter=? WHERE session_id=?",
                 (ts, end_meter, sid))
    conn.commit()
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "session": row_dict(sess)})


def compute_bill(sess, samples):
    """按表码样本把电量切到各费率时段：每段样本区间的电量按时间占比分摊。"""
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
    return round(total_kwh, 3), str(total), items


@app.post("/api/sessions/<sid>/settle")
def settle(sid):
    """结算：一次充电只出一张账单。重复调用返回已生成的账单（幂等）。"""
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not sess:
        conn.close()
        return err("会话不存在", 404)
    existing = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
    if existing:
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "bill": _bill_dict(existing), "duplicated": True})
    if sess["state"] not in ("FINISHED", "SETTLED"):
        conn.rollback()
        conn.close()
        return err(f"当前状态 {sess['state']} 不能结算，请先结束充电", 409)

    samples = [row_dict(r) for r in conn.execute(
        "SELECT seq, ts, kwh FROM meter_samples WHERE session_id=? ORDER BY ts, seq", (sid,))]
    total_kwh, total_amount, items = compute_bill(sess, samples)
    bill_id = "B" + uuid.uuid4().hex[:12]
    try:
        conn.execute(
            "INSERT INTO bills(bill_id, session_id, total_kwh, total_amount, breakdown, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (bill_id, sid, total_kwh, total_amount, json.dumps(items, ensure_ascii=False), now()),
        )
        conn.execute("UPDATE sessions SET state='SETTLED' WHERE session_id=?", (sid,))
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发下另一请求已结算：回滚后返回那张账单，绝不重复出账
        conn.rollback()
        existing = conn.execute("SELECT * FROM bills WHERE session_id=?", (sid,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "bill": _bill_dict(existing), "duplicated": True})
    bill = conn.execute("SELECT * FROM bills WHERE bill_id=?", (bill_id,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "bill": _bill_dict(bill)})


def _bill_dict(row):
    d = row_dict(row)
    d["breakdown"] = json.loads(d["breakdown"])
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
    conn.close()
    out = {"ok": True, "session": row_dict(sess)}
    if bill:
        out["bill"] = _bill_dict(bill)
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
