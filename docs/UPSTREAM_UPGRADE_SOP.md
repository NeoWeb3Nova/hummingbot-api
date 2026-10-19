# Hummingbot 与官方主版本「永久无缝升级」标准作业程序 (SOP)

本文档定义了本工作区中 Hummingbot 定制化开发（包括 `hummingbot-dashboard`、`hummingbot-api` 与 `hummingbot-core`）与官方主版本（Upstream）保持**长期无缝、零冲突平滑升级**的工程架构方案与操作规范。

---

## 一、核心解耦架构与设计哲学 (Architecture Decoupling)

### 1. 传统定制开发的“升级困境”
在开源二次开发中，若直接在官方核心文件（例如 `hummingbot-dashboard` 的 `frontend/pages/orchestration/instances/app.py`）中插入成百上千行的私有业务逻辑（如资金费率套利持仓表格、深度图表、三维对账卡片等）：
- **致命后果**：每当官方发布新版本或重构 UI，执行 `git merge` 或 `git rebase` 时，`app.py` 会产生大面积冲突（Merge Conflict），极易误删官方新增特性或破坏定制逻辑；
- **排查成本**：每次升级都需耗费数小时逐行对齐代码，严重阻碍技术演进。

### 2. 最佳工程解耦模式：自包含组件 + 极简插件插槽
本项目已完成彻底的架构解耦重构：

```
hummingbot-dashboard/
├── frontend/
│   ├── components/
│   │   └── funding_arbitrage_view.py   <--- [核心定制逻辑，完全隔离] 700+ 行
│   │                                       包含全部资金费套利计算、三维卡片、
│   │                                       Plotly 深度图表、多维标签页等。
│   └── pages/orchestration/instances/
│       └── app.py                      <--- [官方主文件，恢复极简]
│                                            仅保留 2~3 行轻量插槽挂载代码
```

#### 官方 `app.py` 中的极简插槽形态：
```python
# 1. 顶部导入独立组件
from frontend.components.funding_arbitrage_view import (
    get_bot_funding_arbitrage_data,
    render_funding_arbitrage_kpi_cards,
    render_funding_arbitrage_tables,
)

# 2. 识别并挂载 KPI 卡片
if is_funding_arb_bot and arb_data:
    render_funding_arbitrage_kpi_cards(arb_data, total_volume_traded, total_global_pnl_quote)

# 3. 识别并挂载详情看板
if is_funding_arb_bot and arb_data:
    render_funding_arbitrage_tables(arb_data, bot_name)
```

#### 为什么此方案能实现“永久无缝升级”？
1. **文件物理隔离**：700+ 行的核心套利代码位于官方仓库**不存在的独立路径**（`frontend/components/funding_arbitrage_view.py`），官方代码库的任何更新都不会触碰该文件，**冲突率为 0%**；
2. **插槽代码极简**：在官方 `app.py` 中仅有 3 处条件判断钩子（Hook），即便官方改版重构了 `app.py`，Git 也能自动智能合并（Auto-merge）；若极特殊情况下发生行号偏移，也只需 10 秒钟快速微调这 3 个调用点；
3. **原生机器人零侵入零污染**：通过 `is_funding_arb_bot` 严密防护盾，非资金费套利机器人（如 PMM 做市、DMM、现货网格）100% 走官方原生逻辑与原生渲染流水线。

---

## 二、双源 Git 拓扑架构 (Dual-Remote Topology)

本工作区下的各个仓库统一配置了双 Remote 架构：
- `origin`：开发者的私有 GitHub 仓库（用于存储所有定制化提交、特性分支）；
- `upstream`：Hummingbot 官方开源 GitHub 主仓库（用于持续追踪官方最新发布）。

| 子项目目录 | Origin (开发者仓库) | Upstream (官方主仓库) |
| :--- | :--- | :--- |
| `hummingbot-dashboard` | `https://github.com/NeoWeb3Nova/dashboard.git` | `https://github.com/hummingbot/dashboard.git` |
| `hummingbot-api` | `https://github.com/NeoWeb3Nova/hummingbot-api.git` | `https://github.com/hummingbot/hummingbot-api.git` |
| `hummingbot-core` | `https://github.com/NeoWeb3Nova/hummingbot.git` | `https://github.com/hummingbot/hummingbot.git` |

---

## 三、一键自动化同步管理工具 (`sync-upstream.sh`)

我们在工作区根目录部署了专业运维工具 [`sync-upstream.sh`](file:///home/neo/projects/hummingbot/sync-upstream.sh)。

### 常用命令与场景

#### 1. 每日状态概览 (`./sync-upstream.sh status`)
实时检查三大核心仓库与官方 upstream 的偏差状态、Ahead/Behind 提交数以及工作区干净度：
```bash
./sync-upstream.sh status
```
*终端将输出精美对齐的状态表格，展示每个仓库超前官方多少个定制提交、落后官方多少个新提交。*

#### 2. 检查官方上游最新动态 (`./sync-upstream.sh check`)
自动 Fetch 官方最新代码，打印官方最新发布的 Commit 清单：
```bash
./sync-upstream.sh check
```

#### 3. 单独拉取上游元数据 (`./sync-upstream.sh fetch`)
仅拉取官方分支元数据，不修改本地任何代码：
```bash
./sync-upstream.sh fetch
```

#### 4. 安全平滑变基升级 (`./sync-upstream.sh rebase <repo_name>`)
将官方最新代码作为底座（Base），将我们的定制特性无缝“嫁接”在官方最新版本之上：
```bash
# 升级 Dashboard
./sync-upstream.sh rebase hummingbot-dashboard

# 升级 API 后端
./sync-upstream.sh rebase hummingbot-api

# 升级 Core 引擎
./sync-upstream.sh rebase hummingbot-core
```

---

## 四、升级遇到冲突时的黄金三步处置法 (Disaster Recovery)

得益于我们的自包含组件架构，绝大多数情况下 `git rebase` 会自动 100% 干净完成。
如果官方恰好大幅度重构了 `app.py` 的结构导致了轻微冲突，按以下标准 SOP 处理：

1. **查看冲突位置**：
   ```bash
   cd hummingbot-dashboard
   git status
   ```
2. **打开冲突文件**（通常仅为 `frontend/pages/orchestration/instances/app.py`），保留官方新的结构，并将我们的插件挂载插槽（`render_funding_arbitrage_kpi_cards` 等）贴入对应的渲染位置；
3. **标记并继续变基**：
   ```bash
   git add frontend/pages/orchestration/instances/app.py
   git rebase --continue
   ```
4. **验证与推送到私有仓库**：
   ```bash
   # 启动或刷新 Dashboard 确认运行正常
   # 强制推送线性提交到个人远程仓库
   git push origin main --force-with-lease
   ```

---

## 五、开发与提交规范准则 (Engineering Discipline)

为了长期保持“无缝升级”的成果，团队后续开发必须严格遵守以下红线：
1. **严禁在官方原生旧文件中堆叠大段新业务**：凡是超过 20 行的新页面组件、新报表逻辑，必须统一建在 `frontend/components/` 或单独的模块文件中；
2. **保持插槽极简**：在官方文件中只写 `import` 和 `if <condition>: render_xxx()`；
3. **升级前先提交**：在执行 `./sync-upstream.sh rebase` 前，务必确认当前工作区 `git status` 处于 Clean 状态；
4. **提交遵循 Conventional Commits**：使用规范前缀（`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`）。
