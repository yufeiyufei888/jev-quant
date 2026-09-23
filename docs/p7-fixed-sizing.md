# P7 固定仓位研究

## 冻结协议

首轮使用2023年完整交易日作为开发段，固定 JEV `jev-1.13.0`、`state_v1`、基准提示词和贵州茅台；80%和50%账户分别运行，不复用另一账户的模型信号。为按用户要求分析模型判断价值，主诊断在当次已完成Bar的收盘价记录成交；延迟成交结果单独报告。协议见 `configs/p7_fixed_sizing_2023_v1.json`。

2022年分钟数据只用于预热；2024作为后续验证段，2025至本地数据截止日作为冻结后的历史检查段。由于这些数据和一部分运行已在开发研究中查看过，不能称为盲测。当前分钟Bar标签、信息可用时间、部分规则和证券状态仍是已声明的模拟假设，因此P7结果属于诊断账户，不是正式历史执行证据。

## 命令

每个仓位运行使用独立输出目录和账本。可在相同参数下用 `--resume` 从最近一个核验检查点继续。P6的20日基准缓存可被80%账户逐请求复用；50%账户状态不同，必须独立请求和记录。

```powershell
$env:PYTHONPATH = "src"
python -m jevquant.p6_sample `
  --minute-root $minuteRoot `
  --daily-csv $dailyCsv `
  --status-2022-2023 $status2023 `
  --status-2024-plus $status2024 `
  --dividends configs/moutai_2023_2024_cash_dividends.json `
  --start 2023-01-03 --sessions 242 `
  --execution-mode instant_snapshot_close --prompt-variant baseline `
  --target-weight 0.80 `
  --output artifacts/p7/fixed-entry-80-2023-v1 `
  --acknowledge-historical-data-to-live-jev
```

完成80%后，以新目录运行完全相同的协议和数据、仅把 `--target-weight` 改为 `0.50`。不得将80%账户中的JEV回答复制给50%账户，因为仓位、现金、浮盈和持有批次会改变后续输入。两账户均需报告账户净值、最大回撤、费用、持仓暴露、交易数、未平仓、API错误及独立对账。

## 运行状态

2023年数据预检已通过：242个开发交易日、21个预热日对应的263份分钟分区全部存在。80%主账户正在连续回放，结果和逐日检查点写入 Git 忽略的 `artifacts/p7/fixed-entry-80-2023-v1/`。完成并对账后，才启动50%独立账户；两个账户完成后再进入有限输入消融和2024验证研究。
