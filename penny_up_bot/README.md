# penny_up_bot —— Polymarket 单边 penny-up 自动建仓工具

针对你指定的市场，**单边 BUY 建仓**，始终把买单贴在「第一档队首」（对手买一价 +1 个 tick）以求优先成交，
同时**严格不超过你设的价格上限**；建仓达到总目标量后自动停止。

> 完全独立、自包含：**零 import 主 bot 任何模块**，只依赖已安装的 `py_clob_client_v2`。
> 请用**另一个 Polymarket 账户**运行（见下），和主 bot 彻底分账户、互不干扰。

## 策略行为

记 `tick`=市场最小价位，`best_comp`=剔除自己后的最高对手买价，`cap`=你设的上限价：

| 盘口 | 动作 |
|---|---|
| 买盘无对手（只有你/空着） | 撤掉自己的单，等待 |
| `best_comp + tick > cap`（对手顶到上限） | 撤单等待 |
| 否则 | 挂 BUY 在 `best_comp + tick`（只超对手一个 tick） |

- 价格**上下都跟**：始终精确等于 `best_comp + tick`，对手撤了就降、被超了就升。
- 挂单量 = 剩余未成交目标；成交累计满总目标 → 撤单、完成。
- **硬上限**：下单价永不 > `cap`。

## 安装 / 配置

1. 填环境变量（**另一个号**的钱包）：
   ```powershell
   copy penny_up_bot\.env.example penny_up_bot\.env
   # 编辑 penny_up_bot\.env，填 PK / BROWSER_ADDRESS（另一个账户），DRY_RUN 先保持 true
   ```

2. 在 [config.py](config.py) 的 `TOKENS` 里填要建仓的市场。方向用「市场 + YES/NO」自动解析：
   ```python
   TOKENS = [
       TokenConfig(
           slug="will-x-happen-by-2026",   # 或 condition_id="0x..."；或直接 token_id="7291..."
           outcome="YES",                   # YES / NO，自动解析成对应 token_id
           cap_price=0.65,                  # 硬上限买价
           total_size=1000.0,               # 总目标建仓量（shares）
           label="Will X happen by 2026?",  # 可选，仅日志显示
       ),
   ]
   ```

## 运行

```powershell
# 在仓库根目录
python -m penny_up_bot.run
```

启动会打印一行**让你肉眼核对方向**再放行：
```
市场: Will X happen by 2026?   买入方向: YES   token_id: 7291...
  上限价: 0.65   目标量: 1000.0 shares   tick: 0.01   neg_risk: False   既有持仓基线: 0.0
  DRY_RUN: true
```

- 默认 `DRY_RUN=true`：只打印将要执行的「撤 / 挂」动作，**不真下单**。先这样观察行为是否符合预期。
- 确认无误后，把 `.env` 的 `DRY_RUN` 改成 `false` 再跑，才会真实下单。
- `Ctrl+C` 退出：**只撤本工具自己挂过的单**，绝不动其它程序/账户的单。

## 隔离保证

- **代码层**：不 import `poly_data` / `execution` / `global_state` / `main.py` / `minimal_auto_order` 等任何主 bot 代码。
- **账户层**：用另一个钱包；撤单一律按 `order_id` 单撤，**永不**调用全局 `cancel_all`。

## 测试

```powershell
python -m pytest penny_up_bot/tests/ -q
```

核心纯逻辑（penny-up 目标价、剔除自己算对手价、成交记账）全部单测覆盖，另有 executor 决策测试与 DRY_RUN 端到端冒烟。

## 模块

| 文件 | 职责 |
|---|---|
| `config.py` | TOKENS 配置 + 全局参数 |
| `resolver.py` | 市场 + YES/NO → token_id（CLOB / Gamma） |
| `client.py` | py_clob_client_v2 轻封装：下单/单撤/查 tick/查持仓 |
| `book_state.py` | 每 token 盘口 + 剔除自己算对手最高买价 |
| `position_state.py` | 每 token 挂单状态 + 成交记账 + 完成判定 |
| `quoting.py` | `compute_target()` penny-up 纯逻辑（核心） |
| `executor.py` | 撤+挂原子化，per-token 锁 + 防重挂 + 去抖 |
| `market_ws.py` / `user_ws.py` | 盘口 / 成交 websocket |
| `reconcile.py` | REST 兜底校正 + 补触发 |
| `run.py` | asyncio 编排 + 优雅退出 |

设计细节见 `docs/superpowers/specs/2026-06-08-penny-up-bot-design.md`。
