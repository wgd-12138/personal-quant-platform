# 持仓数据提供方案

## 结论

东方财富持仓数据分三种接入方式。当前项目先支持 CSV，后续优先接官方量化接口，不保存交易账号密码。

## 方式一：手动 CSV

这是当前马上可用的方式。

| 项目 | 内容 |
|---|---|
| 文件位置 | `data/positions.csv` |
| 必填字段 | `code,name,quantity,cost_price` |
| 可选字段 | `buy_date,available_quantity,position_value,note` |
| 适合场景 | 持仓不频繁变化，先验证报告内容 |
| 风险 | 需要手动更新，盘中买卖后可能不及时 |

示例结构：

```csv
code,name,quantity,cost_price,available_quantity,position_value,note
600000,浦发银行,1000,10.50,1000,10800,真实持仓
```

## 方式二：导出文件自动读取

如果东方财富交易客户端支持把持仓导出为 CSV、XLS 或剪贴板表格，可以做本地自动导入。

| 项目 | 内容 |
|---|---|
| 数据来源 | 东方财富交易客户端导出的持仓文件 |
| 本项目动作 | 监听固定目录，读取最新导出文件，转换成 `data/positions.csv` |
| 刷新频率 | 用户导出一次，系统更新一次 |
| 优点 | 不保存交易账号密码 |
| 缺点 | 不是真正自动实时，需要客户端能导出 |

当前已支持 CSV 导入。

| 目的 | 命令 |
|---|---|
| 导入指定文件 | `python -m personal_quant.cli positions-import --source data/imports/eastmoney_positions.csv --output data/positions.csv` |
| 导入目录中最新文件 | `python -m personal_quant.cli positions-watch --source-dir data/imports --output data/positions.csv --once` |
| 持续监听目录 | `python -m personal_quant.cli positions-watch --source-dir data/imports --output data/positions.csv --interval 10` |

支持的常见列名：

| 标准字段 | 可识别列名 |
|---|---|
| code | code、证券代码、股票代码、代码 |
| name | name、证券名称、股票名称、名称 |
| quantity | quantity、持仓数量、股票余额、证券数量、当前持仓 |
| cost_price | cost_price、成本价、持仓成本、成本价格、参考成本价、买入均价 |
| available_quantity | available_quantity、可用数量、可卖数量、可用余额 |
| position_value | position_value、持仓市值、市值、最新市值 |
| note | note、备注 |

## 方式三：官方量化接口实时读取

这是后续推荐的实时方式。

| 项目 | 内容 |
|---|---|
| 数据来源 | 东方财富官方量化/策略交易环境 |
| 获取内容 | 持仓、可用数量、成本、市值等账户数据 |
| 刷新频率 | 盘中定时查询，例如 5-30 秒一次 |
| 安全边界 | 登录和权限由官方客户端或官方环境处理，本项目不保存账号密码 |
| 前提 | 账户已开通对应量化权限，并能在本机或官方环境运行 |

官方入口：

| 入口 | 链接 |
|---|---|
| 东方财富量化帮助 | https://emt.18.cn/api/quant-help/guide/559.html |

说明：SDK 安装由官方量化终端完成，不在本项目里保存账号密码，也不绕过验证码或短信验证。

## 明确不做

| 不做 | 原因 |
|---|---|
| 保存东方财富账号密码 | 交易账户风险太高 |
| 绕过验证码或短信验证 | 安全和合规风险高 |
| 抓取未公开私有接口 | 容易失效，也可能违反平台规则 |
| 自动下单 | 当前阶段只做报告，不做实盘交易 |

## 当前建议

当前暂不关注持仓个股时，生成报告使用：

```powershell
python -m personal_quant.cli daily-pack --as-of 2026-07-09 --output-dir reports --split --ignore-positions
```

后续重新关注持仓时，再登录官方终端或网页，把持仓导出到 `data/imports/`，用导入命令生成 `data/positions.csv`。
