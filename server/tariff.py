"""峰谷分时电价：费率配置 + 跨时段/跨调价拆分。

电价按场站版本化：每个场站有一条"费率版本时间线"（effective_ts -> 费率表），
把一段充电时间 [start_ts, end_ts] 先按版本生效时刻切开，再按各版本的
时段表切开——一次充电跨过调价时刻时，前后两段各按自己那会儿的价算。
"""
from datetime import datetime, timedelta

# 默认费率表：场站未配置时兜底用。(开始, 结束, 时段名, 电价 元/kWh)
PERIODS = [
    ("00:00", "07:00", "谷", 0.35),
    ("07:00", "08:00", "平", 0.75),
    ("08:00", "11:00", "峰", 1.20),
    ("11:00", "18:00", "平", 0.75),
    ("18:00", "21:00", "峰", 1.20),
    ("21:00", "23:00", "平", 0.75),
    ("23:00", "24:00", "谷", 0.35),
]

SERVICE_FEE = 0.45  # 默认服务费 元/kWh


def price_in(periods, label):
    for _, _, l, p in periods:
        if l == label:
            return p
    raise KeyError(label)


def price_of(label):
    return price_in(PERIODS, label)


def _to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def validate_periods(periods):
    """校验时段表无缝覆盖全天 00:00-24:00。返回错误信息或 None。"""
    spans = sorted((_to_minutes(s), _to_minutes(e)) for s, e, _l, _p in periods)
    if not spans or spans[0][0] != 0:
        return "时段表必须从 00:00 开始"
    for (s, e), (ns, _ne) in zip(spans, spans[1:]):
        if e != ns:
            return "时段表存在空隙或重叠"
    if spans[-1][1] != 24 * 60:
        return "时段表必须覆盖到 24:00"
    return None


def period_at(ts, periods=PERIODS):
    """某一时刻（epoch 秒）所在的费率时段名。"""
    dt = datetime.fromtimestamp(ts)
    minutes = dt.hour * 60 + dt.minute + dt.second / 60.0
    for start_s, end_s, label, _price in periods:
        if _to_minutes(start_s) <= minutes < _to_minutes(end_s):
            return label
    raise ValueError(f"费率表未覆盖时刻 {dt}，请检查时段表是否覆盖全天")


def split_by_periods(start_ts, end_ts, periods):
    """把 [start_ts, end_ts]（epoch 秒）按给定时段表拆分，返回 {时段名: 秒数}。"""
    if end_ts <= start_ts:
        return {}
    result = {}
    t = float(start_ts)
    end_ts = float(end_ts)
    while t < end_ts - 1e-9:
        dt = datetime.fromtimestamp(t)
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes = dt.hour * 60 + dt.minute + dt.second / 60.0 + dt.microsecond / 6e7
        for start_s, end_s, label, _price in periods:
            if _to_minutes(start_s) <= minutes < _to_minutes(end_s):
                period_end = (day_start + timedelta(minutes=_to_minutes(end_s))).timestamp()
                boundary = min(period_end, end_ts)
                result[label] = result.get(label, 0.0) + (boundary - t)
                t = boundary
                break
        else:
            raise ValueError(f"费率表未覆盖时刻 {dt}，请检查时段表是否覆盖全天")
    return result


def split_by_period(start_ts, end_ts):
    """按默认费率表拆分（兼容旧调用）。"""
    return split_by_periods(start_ts, end_ts, PERIODS)


def version_at(versions, ts):
    """时间线上 ts 时刻生效的版本，返回 (periods, service_fee)。早于首版本用首版本。"""
    periods, fee = versions[0][1], versions[0][2]
    for eff, p, f in versions:
        if eff <= ts:
            periods, fee = p, f
        else:
            break
    return periods, fee


def split_by_timeline(start_ts, end_ts, versions):
    """把 [start_ts, end_ts] 先按费率版本生效时刻切、再按各版本时段表切。

    versions: [(effective_ts, periods, service_fee)]，按 effective_ts 升序。
    返回 {(时段名, 电价, 服务费): 秒数}，按首次出现顺序排列——
    同一时段名调价前后会是两个键，账单上自然分成两行。
    """
    if end_ts <= start_ts:
        return {}
    bounds = [start_ts]
    bounds += sorted(e for e, _p, _f in versions if start_ts < e < end_ts)
    bounds.append(end_ts)
    result = {}
    for a, b in zip(bounds, bounds[1:]):
        periods, fee = version_at(versions, a)
        for label, secs in split_by_periods(a, b, periods).items():
            key = (label, price_in(periods, label), fee)
            result[key] = result.get(key, 0.0) + secs
    return result
