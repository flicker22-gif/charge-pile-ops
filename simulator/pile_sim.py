"""充电桩模拟器：上报状态、电表读数，断线/服务端报错时本地持久化、恢复后按序补报。

真实桩的表码是累计值且单调递增。每条样本**先落盘再发送**：本地状态文件
（默认 simulator/.pile_state/<pile_id>.json，原子写）里持久化当前表码、seq、
会话号、未确认样本队列（outbox）和永久拒收的死信档（dead_letter）。

补报链语义：
- 发送失败（连不上、超时、4xx/5xx）整批保留在 outbox，下次（或重启后）重发，
  队列始终按 seq 升序，恢复后严格按原顺序补报；
- 服务端 200 响应里逐条确认：accepted / late 已处理、duplicate 是已收过的重发，
  三种都出队；rejected（会话非法等永久性错误）转死信档落盘，不堵住后面的样本；
- 若请求实际已到服务端但响应丢失，重发后全部按 duplicate 去重，绝不重复计电；
- 桩进程重启后用同一 pile_id 重建 Pile，自动从状态文件恢复表码、seq、会话和队列。
"""
import json
import os
import tempfile
import urllib.error
import urllib.request


class PileOffline(Exception):
    pass


class PileHttpError(Exception):
    """服务端回了 HTTP 错误码（4xx/5xx）：对样本类上报视为未确认，必须留档重发。"""

    def __init__(self, path, code, body):
        super().__init__(f"{path} -> {code}: {body}")
        self.path = path
        self.code = code
        self.body = body


DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pile_state")


class Pile:
    def __init__(self, server, pile_id, name="", state_dir=DEFAULT_STATE_DIR):
        self.server = server.rstrip("/")
        self.pile_id = pile_id
        self.name = name or pile_id
        self.online = True
        self.state_path = os.path.join(state_dir, f"{pile_id}.json")
        self.last_sent = []        # 最近一批已确认样本（用于模拟网络重传）
        self.last_error = None     # 最近一次发送失败（错误码/异常），供演示和测试观察
        self._load_state()

    # ---- 本地持久化 ----
    def _load_state(self):
        """启动/重启时从本地状态文件恢复；没有则从底数开始。"""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.meter_kwh = float(data.get("meter_kwh", 1000.0))  # 电表底数（累计读数，不清零）
        self.session_id = data.get("session_id")
        self.seq = int(data.get("seq", 0))
        self.outbox = list(data.get("outbox", []))            # 服务端尚未确认的样本（按 seq）
        self.dead_letter = list(data.get("dead_letter", []))  # 永久拒收，只留档不再发送
        self.outbox.sort(key=lambda s: s["seq"])

    def _save_state(self):
        """原子写状态文件（同目录临时文件 + replace），写到一半掉电也不会留半截文件。"""
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        data = {
            "pile_id": self.pile_id,
            "meter_kwh": round(self.meter_kwh, 3),
            "session_id": self.session_id,
            "seq": self.seq,
            "outbox": self.outbox,
            "dead_letter": self.dead_letter,
        }
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.state_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def pending(self):
        """还在等服务端确认的样本数。"""
        return len(self.outbox)

    # ---- 通信 ----
    def _http_post(self, path, payload):
        """发一次 HTTP POST。连接失败 -> PileOffline；4xx/5xx -> PileHttpError。"""
        req = urllib.request.Request(
            self.server + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise PileHttpError(path, e.code, e.read().decode())
        except urllib.error.URLError:
            raise PileOffline()

    def _post(self, path, payload):
        return self._http_post(path, payload)

    def _send(self, path, payload):
        """在线则直发，离线则抛 PileOffline（样本类数据由调用方缓存）。"""
        if not self.online:
            raise PileOffline()
        return self._http_post(path, payload)

    # ---- 桩行为 ----
    def register(self):
        return self._post("/api/piles/register", {"pile_id": self.pile_id, "name": self.name})

    def plug_in(self, ts):
        # 上一单还有未确认样本时（极少见，比如没等补报完就拔枪插枪），不丢，转入死信留档
        if self.outbox:
            self._dead_letter(self.outbox, "新会话开始，旧未确认样本留档")
            self._save_state()
        r = self._post(f"/api/piles/{self.pile_id}/event",
                       {"type": "plug_in", "ts": ts, "meter_kwh": self.meter_kwh})
        self.session_id = r["session"]["session_id"]
        self.seq = 0
        self._save_state()
        return r

    def plug_out(self, ts):
        return self._post(f"/api/piles/{self.pile_id}/event", {"type": "plug_out", "ts": ts})

    def report_meter(self, ts):
        """生成一条电表样本：先落盘进补发队列，再尝试 flush。

        返回服务端响应（含 warning 预警 / cmd 断电指令）；
        离线或发送失败时返回 None，样本仍安全留在本地队列里。
        """
        self.seq += 1
        sample = {"seq": self.seq, "ts": ts, "kwh": round(self.meter_kwh, 3)}
        self.outbox.append(sample)
        self._save_state()   # 发送之前先持久化：4xx、掉电、进程被杀都不丢
        return self.flush()

    def flush(self):
        """把未确认样本按 seq 顺序发出去并按服务端逐条回执核销。

        - 发不出去或服务端回 4xx/5xx：整批保留在 outbox（已在盘上），等下次/重启重发；
        - accepted / late：服务端已处理，出队；
        - duplicate：服务端早就收过（上次响应丢失后的重发），同样出队；
        - rejected：永久性拒收（会话非法等），转死信档，不堵队列后面的样本。
        """
        if not self.outbox or not self.online:
            return None
        batch = sorted(self.outbox, key=lambda s: s["seq"])
        try:
            r = self._post(f"/api/piles/{self.pile_id}/meter",
                           {"session_id": self.session_id, "samples": batch})
        except (PileOffline, PileHttpError) as e:
            # 未拿到逐条回执：一条都不能核销，原样留队列（已落盘），返回 None 等重试
            self.last_error = e
            return None
        self.last_error = None
        acked = {x["seq"]: x["status"] for x in r.get("results", [])}
        reasons = {x["seq"]: x.get("reason") for x in r.get("results", [])}
        remaining, dead, confirmed = [], [], []
        for s in batch:
            status = acked.get(s["seq"])
            if status in ("accepted", "late", "duplicate"):
                confirmed.append(s)
            elif status == "rejected":
                # 永久性拒收：转死信档落盘，不堵队列后面的样本
                s2 = dict(s)
                s2["dead_reason"] = reasons.get(s["seq"], "rejected")
                dead.append(s2)
            else:
                # 服务端没给这条回执（异常响应）：保守起见留在队列里下次重发
                remaining.append(s)
        self.outbox = sorted(remaining, key=lambda s: s["seq"])
        if dead:
            self.dead_letter.extend(dead)
        self.last_sent = confirmed  # 已确认的样本才允许作为"网络重传"素材
        self._save_state()
        return r

    def _dead_letter(self, samples, reason):
        for s in samples:
            s2 = dict(s)
            s2["dead_reason"] = reason
            self.dead_letter.append(s2)
        self.outbox = []

    def resend_last(self):
        """模拟网络重传：把最近一批已确认样本原样再发一遍（服务端应全部去重）。"""
        if not self.last_sent:
            return None
        return self._post(f"/api/piles/{self.pile_id}/meter",
                          {"session_id": self.session_id, "samples": self.last_sent})

    def charge(self, kwh):
        """充入 kwh 度电（表码前进）。"""
        self.meter_kwh += kwh
        self._save_state()

    # ---- 断网模拟 ----
    def disconnect(self):
        self.online = False

    def reconnect(self):
        self.online = True
        return self.flush()
