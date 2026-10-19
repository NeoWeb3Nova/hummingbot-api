# Hummingbot 资金费率量化实盘 VPS 部署与生产运维白皮书

本文档系统性评估当前项目架构迁移至云端 VPS（虚拟专用服务器）的便利性，并针对**实盘生产环境（Live Production）**总结了必须提前考量的 **6 大核心要素、技术陷阱与标准部署 SOP**。

---

## 一、当前解耦架构对 VPS 部署的优势

经过模块化与组件解耦改造后，本项目在 VPS 上的部署具有极高的便捷性与高可用性：
1. **“克隆即用”的独立组件体系**：
   - 所有的核心套利业务指标、三维对账看板、流动性安全容量与回本倒计时逻辑全部封装在 [`hummingbot-dashboard/frontend/components/funding_arbitrage_view.py`](file:///home/neo/projects/hummingbot/hummingbot-dashboard/frontend/components/funding_arbitrage_view.py) 中，并已完全推送到个人 GitHub 远端；
   - 在新 VPS 上只需执行 `git clone`，无需进行任何复杂的代码修补（Patching）或配置合并。
2. **零冲突跟随官方演进**：
   - 根目录预置了自动化工具 [`sync-upstream.sh`](file:///home/neo/projects/hummingbot/sync-upstream.sh)；
   - 即使部署在云端服务器，未来官方发布新功能或安全补丁时，仅需运行 `./sync-upstream.sh check` 与 `./sync-upstream.sh rebase` 即可实现 100% 自动无缝变基升级。
3. **原生微服务容器化底座**：
   - 核心交易引擎（`hummingbot` 容器）、后台管理（`hummingbot-api` 容器）、消息总线（`emqx` 容器）与数据库（`postgres` 容器）均已容器化，环境依赖完全隔离。

---

## 二、实盘部署必须附加考量的 6 大关键要素与陷阱防范

从本地“纸面预演（Dry-Run）”切换到 VPS“实盘交易（Live Trading）”时，环境面临从内网到公网、从模拟到真金白银的转变，**以下 6 点绝对不能遗漏**：

### 1. 机房地理选址与低延迟网络 (Geographic Colocation & Low Latency)
- **黄金选址法则**：
  - 强烈建议优先选择 **日本东京（Tokyo）** 或 **新加坡（Singapore）** 节点的 VPS（如 AWS `ap-northeast-1` / `ap-southeast-1`、Hetzner/Vultr/Linode 的东京机房）；
  - **原因**：Binance（币安）撮合服务器核心机房在东京/新加坡，Hyperliquid 的主节点与低延迟验证者网络亦主要分布在亚太和美西。选在东京/新加坡机房，双边交易所 API / WebSocket 往返延迟仅需 **1ms ~ 10ms**，可最大限度减少吃单滑点。
- **两大致命地域红线**：
  - **严禁选择美国本土 VPS**：Binance.com 对来自美国 IP 的请求实行严格的地域封锁（Geo-blocking）。若使用美国 IP，所有合约 API 请求会被直接抛出 `HTTP 451 Unavailable For Legal Reasons` 或 403 阻断！
  - **严禁选择中国大陆机房**：访问境外加密货币交易所会被网络防火墙（GFW）阻断或遭遇严重丢包。
- **商用数据中心 IP 风控注意**：
  - 尽量选择大厂主流云平台（AWS / GCP / Linode / Vultr / Hetzner），避免使用信誉极差的便宜小 IDC 节点，否则可能触发交易所的 Cloudflare 防火墙或验证码拦截。

---

### 2. 公网端口安全防护与零暴露 (Public Security & Zero Exposure)
- **致命隐患**：
  - 本地测试时，Streamlit Dashboard 运行在 `0.0.0.0:8501`，且环境变量常关闭密码认证（`AUTH_SYSTEM_ENABLED=False`）；
  - API 暴露在 8000 端口，MQTT Broker 监听在 1883 端口；
  - **一旦挂上公网 IP，全网黑客扫描器（如 Shodan、Censys）会在几分钟内扫出端口！** 任何人只要在浏览器输入 `http://<VPS_IP>:8501` 就能直接操纵您的对冲策略，甚至通过开放的 API 发送平仓或资金划转指令！
- **生产级安全对策（三选一，强烈推荐方案 A）**：
  - **方案 A（最佳推荐：Tailscale / WireGuard 零信任内网）**：
    - 项目的 `hummingbot-api` 已原生支持 Tailscale（`docker-compose.tailscale.yml`）；
    - VPS 加入私有 Tailnet 后，Dashboard 和 API 只绑定 Tailscale 私有网卡 IP。外部公网完全扫描不到 8501 / 8000 端口，您在电脑或手机上挂着 Tailscale 即可安全访问。
  - **方案 B（极简方案：SSH 端口转发加密隧道）**：
    - VPS 防火墙（`ufw`）拒绝外部访问 8501 / 8000 / 1883 / 5432；
    - 本地终端运行一条端口转发命令：
      ```bash
      ssh -L 8501:localhost:8501 -L 8000:localhost:8000 user@vps_ip
      ```
      直接在个人电脑浏览器访问 `http://localhost:8501`，流量全程通过 SSH 强加密，外界坚不可摧。
  - **方案 C（公网域名 + Nginx 反向代理 + SSL + 强密码认证）**：
    - 绑定个人二级域名，前置 Nginx 配置 HTTPS 证书（Let's Encrypt），开启 HTTP Basic Auth，并强制打开 Dashboard 内置认证开关。

---

### 3. 交易所 API 密钥权限收敛与固定 IP 白名单 (API Least Privilege & Whitelisting)
- **密钥权限最小化原则**：
  - 在 Binance 与 Hyperliquid 创建 API Key 时，**严禁勾选“提现（Withdrawal）”权限**！
  - 仅开通“合约交易（Futures Trading）”与“读取（Read-Only）”权限。即使 VPS 遭遇物理攻击，攻击者也绝无可能将账户资金提走。
- **绑定 VPS 固定公网 IP 白名单**：
  - VPS 购买通常附赠 1 个固定公网静态 IP（Static Public IP）；
  - 务必在 Binance 后台的 API 权限设置中，**将 API Key 严格绑定至该 VPS IP**。这样即使 API Secret 意外泄露，从其他任何 IP 发出的交易请求都会被交易所网关直接拒绝。
- **代码库防泄露**：
  - 确保 `.gitignore` 严格排除了 `hummingbot-api/bots/instances/*/conf/`、`hummingbot_conf.yml`、`credentials/` 与 `*.json`，严防私钥误提至 GitHub。

---

### 4. 高频量化时钟高精度同步 (NTP / Chrony Time Sync)
- **致命机制陷阱**：
  - 币安等主流交易所为了防御重放攻击（Replay Attack），要求所有 API 签名请求附带当前时间戳（`timestamp`），并设置了严格的接收窗口（`recvWindow`，默认 5000 毫秒）；
  - 云端虚拟机的系统时钟容易因 CPU 调度或宿主机负载出现漂移。一旦 VPS 系统时间快了或慢了超过 1~2 秒，发送开仓平仓订单时会报致命错误：`Timestamp for this request was 1000ms outside of the recvWindow`；
  - **极端恶果**：Hyperliquid 腿开仓成功，而币安腿因时钟偏差被拒单，导致原本严密对冲的头寸**瞬间沦为裸多或裸空的单边敞口，面临不可预测的市场风险**！
- **VPS 必做防范配置**：
  在 VPS 初始化阶段必须部署高精度的 `chrony` 授时服务：
  ```bash
  sudo apt-get update && sudo apt-get install -y chrony
  sudo systemctl enable --now chrony
  # 验证时钟同步状态（确保时差在数毫秒级）
  chronyc tracking
  ```

---

### 5. 进程开机自启与故障自愈守护 (Process Supervision & Self-Healing)
- **风险场景**：
  - VPS 遭遇云厂商底层物理机维护重启、内核自动升级，或 Streamlit 偶发内存溢出崩溃。若无守护机制，系统会长期处于离线状态。
- **生产配置规范**：
  - **Docker 容器**：确保 `docker-compose.yml` 中的所有基础容器（`api`, `broker`, `postgres`）均带有 `restart: unless-stopped`（本项目当前配置已满足此项）；
  - **Dashboard Web UI 托管（Systemd 方案）**：
    在 VPS 的 `/etc/systemd/system/hummingbot-dashboard.service` 创建系统服务：
    ```ini
    [Unit]
    Description=Hummingbot Dashboard Web UI
    After=network.target docker.service

    [Service]
    Type=simple
    User=neo
    WorkingDirectory=/home/neo/projects/hummingbot
    ExecStart=/home/neo/projects/hummingbot/start-dashboard.sh
    Restart=always
    RestartSec=5
    Environment=PYTHONUNBUFFERED=1

    [Install]
    WantedBy=multi-user.target
    ```
    执行 `sudo systemctl enable --now hummingbot-dashboard`，实现开机自启、奔溃 5 秒内自动拉起。

---

### 6. 日志暴涨防护与磁盘空间防爆 (Log Rotation & Disk Cleanliness)
- **现象与危害**：
  - 资金费率套利策略高频监听行情，每分钟写入大量调试与交易对账日志；
  - 一般小型 VPS 磁盘空间仅为 40GB ~ 80GB。若不加限制，1~2 个月后日志文件可能膨胀至数十 GB，直至 **磁盘使用率达到 100%**；
  - **严重后果**：Docker 进程假死、PostgreSQL 数据库事务写入中断、订单状态无法落盘，导致策略平仓完全失控！
- **双重防爆机制**：
  1. **Docker 全局日志轮转配置**：
     编辑 VPS 的 `/etc/docker/daemon.json`：
     ```json
     {
       "log-driver": "json-file",
       "log-opts": {
         "max-size": "50m",
         "max-file": "3"
       }
     }
     ```
     重启 Docker (`sudo systemctl restart docker`)，确保单个容器日志最大不超过 150MB。
  2. **自动定时清理 Crontab**：
     利用项目根目录下经过打磨的 [`clean-logs.sh`](file:///home/neo/projects/hummingbot/clean-logs.sh)（它采用 `truncate -s 0` 清空日志，不中断运行中的 Bot）：
     ```bash
     # 在 VPS 上添加每周日凌晨自动清理日志的定时任务
     (crontab -l 2>/dev/null; echo "0 3 * * 0 /home/neo/projects/hummingbot/clean-logs.sh > /dev/null 2>&1") | crontab -
     ```

---

## 三、VPS 首次开荒部署标准作业程序 (SOP)

当您新开通一台全新的 Ubuntu 22.04 / 24.04 VPS 时，按以下极简步骤即可完成全部部署：

```bash
# 1. 基础环境与时钟同步
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ufw chrony
sudo systemctl enable --now chrony

# 2. 安装 Docker & Docker Compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 3. 安装 Miniforge (Python 3.10 环境)
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
bash Miniforge3-Linux-x86_64.sh -b -p $HOME/miniforge3
$HOME/miniforge3/bin/conda init bash
source ~/.bashrc

# 4. 拉取代码库 (带全部解耦套利组件)
mkdir -p ~/projects && cd ~/projects
git clone --recurse-submodules https://github.com/NeoWeb3Nova/dashboard.git hummingbot-dashboard
git clone https://github.com/NeoWeb3Nova/hummingbot-api.git
git clone https://github.com/NeoWeb3Nova/hummingbot.git hummingbot-core

# 5. 初始化与启动后端微服务
cd ~/projects/hummingbot/hummingbot-api
make deploy

# 6. 配置并启动 Dashboard
cd ~/projects/hummingbot/hummingbot-dashboard
conda create -n dashboard python=3.10 -y
conda activate dashboard
pip install -r requirements.txt
cd ~/projects/hummingbot
./start-dashboard.sh
```

---

## 四、生产就绪自检清单 (Production Readiness Checklist)

在 VPS 开启实盘真实资金交易前，请逐一勾选以下安全自检项：

- [ ] **机房**：位于日本东京或新加坡机房，非美国 IP，Ping 币安延迟 `<= 20ms`。
- [ ] **时钟**：`chronyc tracking` 确认与网络原子钟时差 `<= 10ms`。
- [ ] **端口**：`sudo ufw status` 确认 8501、8000、1883 端口未对公网开放。
- [ ] **隧道**：Tailscale 或 SSH 端口转发配置就绪，可顺畅在本地访问 Dashboard。
- [ ] **密钥**：交易所 API Key 已关闭“提现”权限，并绑定了 VPS 静态公网 IP。
- [ ] **守护**：Docker 容器配置了 `restart: unless-stopped`，Dashboard 配好了 Systemd 服务。
- [ ] **日志**：Docker 配置了 `max-size=50m`，并挂载了 `clean-logs.sh` 定时任务。
- [ ] **权限**：项目目录宿主机 UID 为 `1000:1000`，无根属主权限错误。
