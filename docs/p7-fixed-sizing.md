# P7 固定仓位研究

**状态更新（2026-09-26）：**此页保留 P7 当时的协议和运行记录，不再表示当前正在执行。80%账户的既有结果保留；旧50%账户停在2023-05-30 API超时检查点，属于未完成实验。新的多股票短差研究另见 [`p8-short-t.md`](p8-short-t.md)，不沿用或改写 P7 账户。

## 冻结协议

首轮开发段为2023年完整交易日，固定 JEV `jev-1.13.0`、基准提示词和贵州茅台；80%和50%账户分别运行，不复用另一账户的模型信号。主诊断按用户要求在 JEV 当次看到的已完成Bar收盘价记账。`state_v1` 冻结协议见 `configs/p7_fixed_sizing_2023_v1.json`；为节约API用量，后续运行改用新的 `compact_v1` 输入协议 `configs/p7_fixed_sizing_2023_v2_compact.json`，其结果不能与旧输入版本视为完全同条件。

2022年分钟数据只用于预热；2024作为后续验证段，2025至本地数据截止日作为冻结后的历史检查段。由于这些数据和一部分运行已在开发研究中查看过，不能称为盲测。当前分钟Bar标签、信息可用时间、部分规则和证券状态仍是已声明的模拟假设，因此P7结果属于诊断账户，不是正式历史执行证据。

## 命令

每个仓位运行使用独立输出目录和账本。80%账户第一版已因控制调用费用而在2023-03-20安全停止：2,410条成功JEV调用、约$0.959本地估算费用，决策全部WAIT、没有成交。该部分结果不是全年结果；已冻结的state_v1账户不会被改写成compact_v1。新运行从年初以新协议开始，并使用最近的Usage页面金额和时间戳启用$5账户级本地预算门。50%账户依然必须独立请求和记录。

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
  --input-schema compact_v1 `
  --target-weight 0.80 `
  --account-spend-baseline-usd $usageSpend `
  --account-spend-baseline-at $usageTimestamp `
  --account-spend-limit-usd 5.00 `
  --output artifacts/p7/fixed-entry-80-2023-v2-compact-r1 `
  --acknowledge-historical-data-to-live-jev
```

每次实跑前刷新TypeSafe Usage页面，分别把页面总金额和截图/页面时间填入 `$usageSpend`、`$usageTimestamp`。预算门会将该时点之后本项目所有已记费用与下一次请求的保守估算累加，超出$5就停止。完成80%后，以新目录运行相同compact协议、仅把 `--target-weight` 改为 `0.50`。不得把80%账户中的JEV回答复制给50%账户。两账户均需报告账户净值、最大回撤、费用、持仓暴露、交易数、未平仓、API错误及独立对账。

## 当时运行记录（历史保留，不代表当前任务）

2023年数据预检已通过：242个开发交易日、21个预热日对应的263份分钟分区全部存在。旧state_v1回放保留在 `artifacts/p7/fixed-entry-80-2023-v1/`；它在2023-03-20检查点安全停止。compact输入映射的第一次调用前校验失败，没有发出请求；修正后，v2-r1已推进至2023-01-06，出现1笔BUY并按同一快照价买入400股，独立账户净值为CNY 1,002,093.05。全年仍在运行，不能据此判断策略效果；完成80%并对账后再启动50%账户。
