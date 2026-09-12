# 充电站运营系统（演示版）

桩接进来上报状态，车主扫码充电，按 **插枪 → 充电中 → 结束 → 结算** 走完整流程；
计费支持峰谷分时，跨时段按实际占比分开算；桩中途掉线不丢电，同一次充电不会重复结算。

## 结构

```
server/
  app.py       # Flask + SQLite 后端：桩接入、充电流程、计费、结算
  tariff.py    # 峰谷平费率表 + 跨时段拆分（运营方直接改 PERIODS / SERVICE_FEE）
simulator/
  pile_sim.py  # 充电桩模拟器：状态/表码上报，断线缓存、恢复补报
demo.py        # 端到端演示：一辆车从扫码到出账单
tests/
  test_billing.py  # 拆分、补报去重、结算幂等的单元测试
```

## 运行

```bash
python3 demo.py                          # 一键演示（自动起服务端，约 10 秒看完一整夜充电）
python3 server/app.py --port 5000        # 单独起服务端
python3 -m unittest discover -s tests    # 跑测试
```

只依赖 Flask，其余全部标准库。

## 关键设计

**分时计费**：桩按固定间隔上报电表**累计读数**（带时间戳）。结算时对每两个相邻
样本之间的时间段调用 `tariff.split_by_period` 按费率边界切开，该段电量按各时段
时长占比分摊，再分别乘该时段电价 + 服务费。跨零点、跨多个时段都按实际占比算。

**掉线不丢电**：表码是单调递增的累计值。桩断线时样本缓存在本地（`Pile.buffer`），
恢复后批量补报；即使中间报文全丢，末次表码减起始底数仍是全部电量。

**结算窗口**：结算只计入 `ts <= end_ts` 的样本——结束前产生、只是晚到的补报照常
入账；结束之后才产生的样本（例如断线桩的滞后报文）不影响本单。结算时按窗口内
样本重新定格 `end_meter`，保证会话表码与账单一致。

**不重复计**：
- 电表样本按 `(session_id, seq)` 唯一约束去重，补报/网络重发直接 `INSERT OR IGNORE`；
- 结算按 `bills.session_id` 唯一约束保证一次充电只有一张账单，重复结算返回原账单
  （`duplicated: true`），并发下靠 `BEGIN IMMEDIATE` + 唯一索引兜底；
- 插枪、扫码同样是幂等的，重复调用返回已存在的会话。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/piles/register` | 桩注册/上线 |
| POST | `/api/piles/<id>/event` | `plug_in` 插枪建会话 / `plug_out` 拔枪 |
| POST | `/api/piles/<id>/scan` | 车主扫码，PLUGGED → CHARGING |
| POST | `/api/piles/<id>/meter` | 表码上报 `{session_id, samples:[{seq,ts,kwh}]}` |
| POST | `/api/sessions/<id>/stop` | 结束充电，CHARGING → FINISHED |
| POST | `/api/sessions/<id>/settle` | 结算出账单（幂等） |
| GET  | `/api/sessions/<id>` | 会话 + 账单 |
| GET  | `/api/piles` · `/api/tariff` | 桩列表 · 费率表 |

## 后续可扩展

- 费率表目前写在 `tariff.py`，可挪到数据库按站配置；
- 桩与服务端之间现在是简单 HTTP，可换成 OCPP 1.6/2.0.1（接口语义已对齐：
  累计表码、带序号样本、幂等事务）；
- 结算后接支付/发票；账单金额用分存储避免浮点（当前结算时才转 Decimal 舍入到分）。
