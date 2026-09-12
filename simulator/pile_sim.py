"""充电桩模拟器：上报状态、电表读数，支持断线缓存、恢复补报。

真实桩的表码是累计值且单调递增，断线期间样本缓存在本地，
恢复后批量补报；服务端按 (session_id, seq) 去重，重发不重复计。
"""
import json
import urllib.request
import urllib.error


class PileOffline(Exception):
    pass


class Pile:
    def __init__(self, server, pile_id, name=""):
        self.server = server.rstrip("/")
        self.pile_id = pile_id
        self.name = name or pile_id
        self.online = True
        self.meter_kwh = 1000.0   # 电表底数（累计读数，不清零）
        self.session_id = None
        self.seq = 0
        self.buffer = []          # 断线期间缓存的样本
        self.last_sent = []       # 最近一批已发送样本（用于模拟重发）

    # ---- 通信 ----
    def _post(self, path, payload):
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
            raise RuntimeError(f"{path} -> {e.code}: {e.read().decode()}")
        except urllib.error.URLError:
            raise PileOffline()

    def _send(self, path, payload):
        """在线则直发，离线则抛 PileOffline（样本类数据由调用方缓存）。"""
        if not self.online:
            raise PileOffline()
        return self._post(path, payload)

    # ---- 桩行为 ----
    def register(self):
        return self._post("/api/piles/register", {"pile_id": self.pile_id, "name": self.name})

    def plug_in(self, ts):
        r = self._post(f"/api/piles/{self.pile_id}/event",
                       {"type": "plug_in", "ts": ts, "meter_kwh": self.meter_kwh})
        self.session_id = r["session"]["session_id"]
        self.seq = 0
        return r

    def plug_out(self, ts):
        return self._post(f"/api/piles/{self.pile_id}/event", {"type": "plug_out", "ts": ts})

    def report_meter(self, ts):
        """生成一条电表样本；离线时缓存，上线后随 flush 一起补报。"""
        self.seq += 1
        sample = {"seq": self.seq, "ts": ts, "kwh": round(self.meter_kwh, 3)}
        self.buffer.append(sample)
        self.flush()

    def flush(self):
        """把缓存样本发出去；发送失败（仍离线）则保留缓存等下次。"""
        if not self.buffer or not self.online:
            return None
        pending, self.buffer = self.buffer, []
        try:
            r = self._post(f"/api/piles/{self.pile_id}/meter",
                           {"session_id": self.session_id, "samples": pending})
            self.last_sent = pending
            return r
        except PileOffline:
            self.buffer = pending + self.buffer
            return None

    def resend_last(self):
        """模拟网络重传：把最近一批样本原样再发一遍（服务端应去重）。"""
        if not self.last_sent:
            return None
        return self._post(f"/api/piles/{self.pile_id}/meter",
                          {"session_id": self.session_id, "samples": self.last_sent})

    def charge(self, kwh):
        """充入 kwh 度电（表码前进）。"""
        self.meter_kwh += kwh

    # ---- 断网模拟 ----
    def disconnect(self):
        self.online = False

    def reconnect(self):
        self.online = True
        self.flush()
