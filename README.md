# JevQuant

JevQuant 是一个面向 A 股多标的、多账户的量化研究与模拟交易项目。

项目计划逐步开展历史行情回放、策略与基准比较、账户及公司行动核算，并探索 JEV 模型在研究中的辅助作用；通过统一的数据时点、交易规则、成交模拟和账本记录，让每笔模拟决策都可以追溯。

项目使用虚拟账户，不连接真实券商。当前项目尚在初期建设阶段，尚无历史回测结果或盈利结论。首个垂直样例按方案使用贵州茅台 `600519.SH` 和 100 万元虚拟账户；这不限制平台后续支持其他股票与账户配置。

先完成数据与账户验证，再开展策略研究，最后评估实时模拟。

## 当前状态

- P0 已对茅台 1,032 个五分钟分区完成只读全量审计；49 个时间标签/日的规律和成交量单位已核验。详细本机路径及审计明细留在 Git 忽略目录。
- P1/P2 核心账本、成交代理及合成案例可用，含独立现金对账。
- P4 已实现非 JEV 基准账户回放器及 100 个随机种子的套件入口；当前只通过合成账户测试，尚未产生真实行情基准结果。
- P5 完成一次合成状态的真实 JEV 连通与响应校验；另有两日 Mock 决策至净值链。全历史收益和前向模拟尚未执行。
- JEV API 不设调用次数或费用硬上限；记录调用模型、token 与估算费用，不自动按费用停机。
- 正式历史回放仍受分钟时间戳语义、逐日交易状态及公司行动映射等数据验证项阻断；具体见 `docs/data-contract.md`。

基准回放的输入契约和限制见 [`docs/baseline-replay.md`](docs/baseline-replay.md)。

## 本地开发

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
jevquant doctor
python -m pytest -q
```

真实行情路径通过本机环境变量 `JEVQUANT_DATA_ROOT` 提供。不要把行情、运行结果、缓存或凭据提交到仓库。离线测试不访问 JEV。

JEV 凭据可通过 `TYPESAFE_API_KEY` 环境变量提供，也可用本机变量 `JEVQUANT_TYPESAFE_ENV_FILE` 指向已有私有 `.env`；程序只读密钥字段，不复制或输出密钥。
