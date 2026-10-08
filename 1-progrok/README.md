# 1-progrok (Automated Registration & Camoufox Solver)

> 全自动 Grok 批量产号与指纹解盾系统，搭载 **Camoufox 真实指纹本地求解器**、**Cloudflare D1 邮件路由池**、**12+ 域名动态冷却调度** 与 **Grok2API 一键账号池同步**。

---

## 核心架构与技术演进

### 从“纯协议模拟”到“Camoufox 真实指纹有头解盾”

在系统演进早期，我们曾采用纯 HTTP 协议抓包模拟注册接口，并配合第三方打码平台（如 YesCaptcha）获取 Turnstile Token。但在 xAI 升级安全策略后，遭遇了系统性失效：

1. **纯协议被彻底封死**：
   - Cloudflare Turnstile 将验证挑战与浏览器环境底层特征（Canvas 绘制噪点、WebGL 着色器特征、AudioContext 频响、Navigator 参数、鼠标物理贝塞尔轨迹、TLS Client Hello JA3/JA4 签名）强绑定。
   - 第三方打码平台在独立外部环境中获取的纯 Token 提交至 xAI 后，频繁报错（历史生产记录中产生 **864 次 `ERROR_CAPTCHA_UNSOLVABLE`**，占比 24.2%），纯协议方案全线崩溃且打码费用极为高昂。
2. **转向 Camoufox 本地真实指纹解盾器**：
   - 全面引入基于 Firefox C++ 内核深度魔改的 **Camoufox**。
   - **C++ 源码级指纹混淆**：自动在渲染底层注入真实的硬件噪点，彻底移除 `navigator.webdriver` 标记，完美欺骗 Cloudflare 自动化探测。
   - **本地多线程轻量服务 (`:5072`)**：单台服务器即可并发运行 3+ 线程求解，平均单次解盾时间稳定在 2~4 秒，成功率 > 99%，**完全免除第三方打码费用**。

---

## 基础设施与运行机制

### 1. 代理网络架构与出口策略
- **Mihomo 代理池对接**：
  - 本地运行 Mihomo（Clash Meta）内核，监听 `http://127.0.0.1:20172`，统一汇聚海量出口节点。
  - 支持按注册批次与账号粒度动态轮换出口节点。
- **出口 IP 频控与并发限制**：
  - xAI 对单个出口 IP 设有严格的注册频率上限。同一 IP 短时间内若频繁发起注册，会立即触发 Cloudflare WAF 质询升级或 xAI 接口 429 频控。
  - 系统在请求前执行健康探测与请求节流，严禁同一代理出口连续高并发轰炸。
- **TLS 握手断连容错 (curl 35)**：
  - 针对海外 VPS 或住宅代理常见的 TLS 握手抖动（历史日志中发生 **107 次** `curl 35: Connection reset by peer`，占比 3.0%），内置 5 秒超时快速熔断、指数退避重试与故障节点临时隔离。

---

### 2. 邮箱系统与多域名轮换池
- **Cloudflare Email Routing + Catch-all 接收**：
  - 在 Cloudflare 域名控制台开启 Catch-all 规则，任意随机前缀邮件直接交由 Cloudflare Worker 接收并写入 D1。
- **12+ 域名动态池与小时级频控瓶颈**：
  - **实测域名池**：系统维护包含 `example1.space`, `example2.online`, `example3.bond`, `example4.org`, `example5.site`, `example6.xyz`, `example7.ua`, `example8.shop` 等在内的 12+ 域名轮换池。
  - **单域名小时频控阈值**：单个邮箱域名 1 小时内注册超过约 15~20 个账号，xAI 将直接触发 `email-signup-unavailable` 封锁（历史 3,577 次失败中占 **2,600 次即 72.7%**！）。
  - **应对机制**：调度器按批次在 12+ 域名池中均衡轮换，并引入 3000ms stagger 错峰延迟，将单域名每小时注册频次严格压制在安全阈值以内。
- **域名后缀黑名单防坑**：
  - xAI 对特定低价/免费 TLD（如 `.in` 后缀，实测 `missing.indevs.in`）实施直接拒绝策略，返回 `email-domain-rejected`（历史 5 次）。系统前置内置黑名单过滤，严防无效请求。

---

### 3. Cloudflare D1 (SQLite) 邮件库与防爆修剪
- **数据库架构**：采用 Cloudflare 原生分布式 Serverless SQLite 数据库（D1），单库提供 5 GB 免费存储额度。
- **容量健康度监控**：生产数据库（`local_mailbox`）实测容量占用仅 **43.25 MB**（仅占 5 GB 额度的 **0.86%**），余量充裕。
- **双重防爆自动修剪机制**：
  1. **Worker 入库即时修剪**：每次新邮件写入时，自动异步执行 SQL 删除 24 小时前包含 `Grok` 验证码的历史邮件。
  2. **Cron Trigger 定时巡检**：配置每 30 分钟一次的定时任务，周期性清理过期与孤儿邮件记录，彻底防止数据库存储爆满。

---

## 历史生产踩坑量化复盘

| 错误特征 | 发生次数 | 占比 | 根本原因 | 工业级解决方案 |
| :--- | :---: | :---: | :--- | :--- |
| **`email-signup-unavailable`** | **2,600** | **72.7%** | 单域名 1 小时注册数触顶（超 15~20 次） | 12+ 域名池轮换 + 3000ms 错峰调度 |
| **`YesCaptcha ERROR_CAPTCHA_UNSOLVABLE`** | **864** | **24.2%** | 早期三方打码无法通过 xAI 新版 Turnstile 指纹校验 | 全面切换至本地 Camoufox 真实指纹求解器 |
| **`TLS / Proxy Connect Blip (curl 35)`** | **107** | **3.0%** | 代理出口瞬时断连 (`Connection reset by peer`) | 指数退避重试 + 5s 快速熔断 + 节点健康检测 |
| **`email-domain-rejected`** | **5** | **0.1%** | 域名后缀在 xAI 全局黑名单中（如 `.in`） | 前置域名白名单与高危后缀过滤 |
| **`Locator Timeout`** | **1** | **<0.1%** | 极端网络卡顿导致 DOM 渲染超时 | 增加元素加载超时等待与容错重试 |

---

## 目录结构

```
1-progrok/
├── backend/                   # FastAPI 后端服务与调度流水线
│   ├── app.py                 # API 入口与批次注册控制
│   ├── account_pipeline.py    # 账号生成、邮箱轮询与导入流水线
│   ├── grok_build_adapter.py  # xAI 注册底层协议适配
│   ├── moemail.py             # Cloudflare D1 临时邮箱协议对接
│   ├── proxy_pool.py          # Mihomo 代理轮询与健康检测
│   └── requirements.txt       # 后端 Python 依赖
├── turnstile-solver/          # 本地 Camoufox Turnstile 人机验证求解器
│   ├── api_solver.py          # 求解器 HTTP API (:5072)
│   ├── browser_configs.py     # Camoufox C++ 反指纹注入与环境配置
│   ├── Dockerfile             # 求解器容器化构建
│   └── run_solver.sh          # 求解器启动脚本
├── vendor/                    # 底层依赖客户端组件
│   └── grok-build-auth/       # xAI 核心底层协议客户端 (xconsole_client)
├── web/                       # 前端管理控制台静态资源
│   └── static/                # HTML/JS/CSS 页面
├── config.example.json        # 配置文件模板
├── Dockerfile                 # ProGrok 后端容器化构建
├── install_and_start.cmd      # Windows 一键安装并启动脚本
├── start.cmd / start.ps1      # Windows 启动脚本
├── stop.cmd / stop.ps1        # Windows 停止脚本
└── README.md                  # 本文档
```

---

## 快速上手

### 1. 配置准备

```bash
# 复制配置文件
cp config.example.json config/config.json
```

修改 `config/config.json`：
- `mail_base_url` / `mail_domains`：配置 Cloudflare Worker 邮箱接口与托管域名列表。
- `grok2api_base_url`：配置配套的 Grok2API 服务地址（如 `http://127.0.0.1:3001`）。
- `grok2api_admin_password`：配置 Grok2API 管理员密码以开启自动同步。
- `proxy_pool_url`：配置本地 Mihomo 代理池地址（如 `http://127.0.0.1:20172`）。

### 2. 启动本地验证码求解器 (`turnstile-solver`)

```bash
cd turnstile-solver

# 安装依赖并拉取 Camoufox 浏览器二进制
pip install -r requirements.txt
python -m camoufox fetch

# 启动本地求解器服务（监听 5072 端口，启动 3 线程并发）
python api_solver.py --browser_type camoufox --thread 3 --host 0.0.0.0 --port 5072
```

### 3. 启动 ProGrok 控制后台

```bash
cd ../backend

# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务（监听 3080 端口）
python -m uvicorn app:app --host 0.0.0.0 --port 3080
```

打开浏览器访问 `http://localhost:3080` 进入可视化管理面板，点击「开始注册」即可开始全自动产号与同步。
