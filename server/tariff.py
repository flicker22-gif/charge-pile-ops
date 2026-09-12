"""峰谷分时电价：费率配置 + 跨时段拆分。

把一段充电时间 [start_ts, end_ts] 按费率时段边界切开，
每个时段占多少秒就分得多少比例的电量，保证跨时段充电
（例如夜里谷时开始、早上平时结束）能按实际各占多少分开计费。
"""
from datetime import datetime, timedelta

# 费率时段表：(开始, 结束, 时段名, 电价 元/kWh)
# 运营方可直接改这张表，时段按一天内时间划分，覆盖全天 24 小时即可。
PERIODS = [
    ("00:00", "07:00", "谷", 0.35),
    ("07:00", "08:00", "平", 0.75),
    ("08:00", "11:00", "峰", 1.20),
    ("11:00", "18:00", "平", 0.75),
    ("18:00", "21:00", "峰", 1.20),
    ("21:00", "23:00", "平", 0.75),
    ("23:00", "24:00", "谷", 0.35),
]

SERVICE_FEE = 0.45  # 服务费 元/kWh，各时段一致，可按站改

_PRICE_BY_LABEL = {label: price for _, _, label, price in PERIODS}


def price_of(label):
    return _PRICE_BY_LABEL[label]


def _to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def split_by_period(start_ts, end_ts):
    """把 [start_ts, end_ts]（epoch 秒）按费率时段拆分。

    返回 {时段名: 秒数}，各时段秒数之和等于 end_ts - start_ts。
    """
    if end_ts <= start_ts:
        return {}
    result = {}
    t = float(start_ts)
    end_ts = float(end_ts)
    while t < end_ts - 1e-9:
        dt = datetime.fromtimestamp(t)
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes = dt.hour * 60 + dt.minute + dt.second / 60.0 + dt.microsecond / 6e7
        for start_s, end_s, label, _price in PERIODS:
            if _to_minutes(start_s) <= minutes < _to_minutes(end_s):
                period_end = (day_start + timedelta(minutes=_to_minutes(end_s))).timestamp()
                boundary = min(period_end, end_ts)
                result[label] = result.get(label, 0.0) + (boundary - t)
                t = boundary
                break
        else:
            raise ValueError(f"费率表未覆盖时刻 {dt}，请检查 PERIODS 是否覆盖全天")
    return result
