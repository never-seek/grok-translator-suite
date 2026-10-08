# ProGrok 自动化注册系统架构与运维排查手册 (MAINTENANCE_GUIDE)

> **版本**：2026-10-04 生产加固版 (v2.0)  
> **适用范围**：ProGrok 批量注册系统、Mihomo 代理调度、Camoufox 浏览器过盾、OAuth Device Flow 链路、生产翻译隔离保护

---

## 一、 系统架构与端口拓扑

服务器运行着多套相互隔离的服务。**严禁任何注册操作干扰生产翻译服务**。

```
                         [ 外部客户端 / 用户浏览器 ]
                                    │
       ┌────────────────────────────┴─────────────────────────────┐
       │ (管理/注册面板)                                           │ (API 生产调用)
       ▼ 3080                                                     ▼ 3001 / 3002
┌───────────────────────┐                               ┌───────────────────────┐
│ ProGrok Web & API     │                               │ chenyme-grok2api(3001)│
│ (FastAPI / Uvicorn)   │                               │ grok_audit_proxy(3002)│
└──────────┬────────────┘                               └──────────┬────────────┘
           │                                                       │
  ┌────────┼─────────────────┐                                     │
  │        ▼ 5072            ▼ 20172                               ▼ 7897 / 直连
  │  ┌───────────────┐ ┌──────────────────┐             ┌─────────────────────┐
  │  │ Turnstile     │ │ Mihomo ProGrok   │             │ 生产翻译独立通道    │
  │  │ Local Solver  │ │ 75 优质专线池    │             │ (严禁受注册池影响)  │
  │  │ (Camoufox)    │ │ (ProGrok-Auto)   │             └─────────────────────┘
  │  └───────────────┘ └────────┬─────────┘
  ▼                             │
┌───────────────────────┐       │
│ Camoufox 注册执行体   ├───────┘
│ (无头浏览器环境)      │
└───────────────────────┘
```

### 核心端口一览表

| 端口 | 服务名称 | 进程命令 / 描述 | 关键约束与说明 |
| :--- | :--- | :--- | :--- |
| **3080** | **ProGrok Web** | `uvicorn app:app --port 3080` | 控制面板与批量调度中心，由 supervisor 守护 |
| **5072** | **Turnstile Solver** | `api_solver.py --browser_type camoufox --thread 3` | 本地过盾服务，毫秒级返回 cf_clearance / token |
| **20172** | **ProGrok 专线代理** | Mihomo 入口（`ProGrok-Auto` 自动选优） | **ProGrok 注册专用端口**，聚合 75 个全绿优质专线 |
| **20171** | **Novelpia 翻译代理** | Mihomo 入口（`sticky_rotator.py` 调度） | **Novelpia 专用端口**，频繁轮换，**严禁用于注册** |
| **3001** | **chenyme-grok2api** | 生产翻译主服务 | **核心生产业务，严禁重启或占用** |
| **3002** | **grok_audit_proxy** | 生产审计代理 | **核心生产业务，严禁重启或占用** |
| **3000** | **grokcli2api** | 本地 grokcli API 转接 | 辅助 API |
| **3005** | **grokapi-shim** | 负载均衡与并发调度 | 上游调度 |
| **8085** | **Sub2API** | 订阅与账号分发管理 | 注册完成账号自动导入此服务 |
| **9090** | **Mihomo REST API** | Clash External Controller | 节点测速、分组切换管理 API（Secret: `<YOUR_MIHOMO_SECRET>`） |

---

## 二、 完整注册链路与双阶段设计

账号注册分为 **Camoufox 真实浏览器阶段** 和 **OAuth Device Flow 授权阶段**：

### 阶段一：网页注册与 SSO 凭据获取（Camoufox 浏览器）
1. **启动浏览器**：`camoufox_register_adapter.py` 通过 `AsyncCamoufox` 启动无头实例，强制走 `http://127.0.0.1:20172`。
2. **异步求解 Turnstile**：同时并发请求 `http://127.0.0.1:5072/turnstile` 预先获取验证码 Token（通常 0.03s 命中）。
3. **打开注册页并生成 Castle.io 指纹**：
   - 访问 `https://accounts.x.ai/sign-up?redirect=grok-com`。
   - 输入邮箱，Camoufox 真实触发 Castle.io 行为指纹收集，生成 `castleRequestToken`。
   - 点击“Sign up”请求下发邮箱验证码。
4. **Cloudflare 邮箱收信**：
   - 调用 `local-mailbox.your-worker.workers.dev` 轮询邮件。
   - 通常 2~4 秒内收到 6 位数字验证码（如 `SpaceXAI confirmation code: 672-788`）。
5. **提交账号创建**：
   - 填入 6 位验证码，附带已解出的 Turnstile Token，提交 `/create-account`。
6. **提取 SSO Cookie**：
   - 创号成功后，直接从浏览器 Cookie 容器中提取 `sso` 或 `sso-rw`。

### 阶段二：OAuth Device Flow 凭据授权与导入
1. **获取 Device Code**：
   - 后端向 `https://auth.x.ai/oauth2/device/code` 请求设备流授权码。
   - 客户端 ID: `b1a00492-073a-47ea-816f-4c329264a828`。
2. **浏览器完成单点授权**：
   - Camoufox 在当前已登录 SSO 的上下文中直接访问 `verification_uri_complete`。
   - 自动点击页面中的可见提交按钮：`button[type='submit']:has-text('Continue')`。
3. **轮询交换 Token**：
   - 调用 `poll_token` 轮询 `https://auth.x.ai/oauth2/token`。
   - 取得 `access_token` 与 `refresh_token`。
4. **账号入库与分发**：
   - 写入 `/workspace/progrok/runtime/data/accounts/{email}.json`。
   - 写入 `/workspace/progrok/runtime/data/auth.json`。
   - 调用 Sub2API 接口将账号自动上架到生产可用池。

---

## 三、 核心关键点、致命坑点与防御机理

### 1. Castle.io Token 真实性与发信风控（核心底线）
- **现象与机理**：
  - xAI 接入了 **Castle.io** 风险对抗。
  - **致命陷阱**：如果在 Camoufox 网络层拦截剥离或篡改 `castleRequestToken`，xAI 的 HTTP 接口虽然仍会返回 `200 OK`，但其后端风控系统会判定请求异常，**静默拦截、绝对不会发出真实邮件**！
- **修复与规范**：
  - 绝对不可拦截删除 `castleRequestToken`。必须让 Camoufox 真实执行前端 JS SDK，正常采集硬件、Canvas、WebAudio 指纹并随请求送达 xAI。

### 2. xAI 400 `account:email-signup-unavailable` 根因与节点轮换
- **现象与机理**：
  - 点击注册时 xAI 接口返回 `400 {"error":"account:email-signup-unavailable"}`。
  - 此报错并非单纯的前端快速连击，而是**当前出口节点 IP** 被 xAI 识别或处于频控冷却期。在同一时刻，干净 IP（如 `103.151.173.91`）100% 成功，被标记的 IP 则 100% 报 400。
- **修复方案**：
  - `camoufox_register_adapter.py` 内建 **400 频控自动重试机制**：如果首个出口节点返回 400，自动刷新当前页面，通过 Mihomo 负载均衡池轮换至下一个出口 IP 重新请求（最多重试 2 次），彻底避免单节点偶发频控导致批量任务失败。

### 3. 邮箱前缀生成与 MX 路由匹配
- **现象与机理**：
  - 邮箱前缀若包含大写字母、破折号或非标字符，极易被反垃圾算法静默归档或拦截。
  - 若邮箱域名缺少 Cloudflare MX 记录或未挂载邮件接收 Worker，邮件会在 DNS 层面被丢弃。
- **规范与禁忌**：
  - **前缀规范**：固定使用 12 位随机小写字母数字（如 `3qzn0mxo6pv4`），与系统历史上成功注册的 1.35 万个账号保持高度一致。
  - **域名白名单**：生产验证通过的域名为 `rejected-example.online` 与 `example1.space`，邮件在 3 秒内必达。
  - **致命禁忌**：`cooldown-example.bond` 缺少 MX 路由，绝不可在注册时选用。

### 4. 邮箱 API 配置兜底与 JSON 解析健壮性
- **现象与机理**：
  - 历史废弃域名 `https://maliapi.215.im` 已失效并返回 HTML 302 重定向。若前端提交空配置或被 Pydantic 默认值覆盖，`resp.json()` 会直接抛出 `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`，导致 `/api/register` 返回 400。
- **修复方案**：
  - `_CfMailResponse.json()` 增加异常捕获，非 JSON 内容安全降级返回 `{}`；
  - `app.py`、`moemail.py`、`config.py` 全面淘汰 `maliapi.215.im`，统一绑定生产 Worker：`https://local-mailbox.your-worker.workers.dev`；
  - `start_register` 接口逻辑改为合并持久化配置与提交参数，并在接收到 `maliapi` 时自动防御性纠偏。

### 5. OAuth Device Flow 授权页面适配与会话生命周期
- **授权按钮选择器**：
  - accounts.x.ai 更新了授权确认页面结构，原选择器未限定可见的表单提交按钮。已优化为优先匹配 `button[type='submit']:has-text('Continue')`，适配页面变化。
- **单会话生命周期防误杀**：
  - 修复单任务（`batch_id=None`）在 30 秒内被 `reclaim_orphan_sessions` 误清理的问题，单会话享有完整的 180s 宽限期。

### 6. 端口隔离与 20171 污染防御
- **现象与机理**：
  - 端口 `20171` 是 Novelpia 翻译任务专用（由 `sticky_rotator.py` 频繁切换），若注册流量误入，会导致 TLS 握手频繁崩溃（BoringSSL error 35）。
- **修复方案**：
  - 后端在进入注册流程前对所有代理配置进行防御性重写：若包含 `20171`，强制替换为注册专线池 `http://127.0.0.1:20172`。

---

## 四、 核心代码修改位置索引

若未来需要代码审查或二次开发，重点关注以下文件与改动：

### 1. `/workspace/progrok/backend/camoufox_register_adapter.py`
- `run_camoufox_registration(...)`：
  - 保留完整 Castle Token 上报链路；
  - 增加 400 `account:email-signup-unavailable` 页面刷新与节点轮换重试；
  - OAuth 授权点击逻辑适配 `button[type='submit']:has-text('Continue')`；
  - 强制代理清洗（`20171` -> `20172`）。

### 2. `/workspace/progrok/backend/grok_build_adapter.py`
- `_prepare_registration_session(...)`：
  - 邮箱前缀生成逻辑采用 12 位纯小写字母数字（`secrets.choice(string.ascii_lowercase + string.digits)`）；
- `_clean_old_sessions()` / `reclaim_orphan_sessions()`：
  - 单会话享有 180s 宽限期，避免并发或复杂网络下被过早回收。

### 3. `/workspace/progrok/backend/moemail.py`
- `_CfMailResponse.json()`：
  - 增加安全解析与异常捕获，杜绝 `Expecting value` 崩溃；
- `create_mailbox(...)` / `cfmail_list_domains(...)`：
  - 默认使用生产 Worker 邮箱接口，确保验证码毫秒级就绪。

### 4. `/workspace/progrok/backend/app.py`
- `@app.post("/api/register")`：
  - 动态合并持久化配置与提交参数，防御性纠偏代理端口与邮箱 API 地址。

### 5. `/workspace/bin/supervise-services.sh`
- 系统全局守护进程脚本。ProGrok、Mihomo、Solver 等服务由其守护，异常退出 5 秒内自动拉起。

---

## 五、 常用运维命令速查

### 1. 查看服务与进程状态
```bash
# 查看所有关键核心服务进程
ps aux | grep -E 'app:app|mihomo|api_solver|chenyme|supervise' | grep -v grep

# 检查关键监听端口
ss -tlpn | grep -E '20171|20172|3080|5072|3001|3002'
```

### 2. 平滑重启 ProGrok Web 后端（修改 Python 代码后）
由于 `supervise-services.sh` 始终在后台循环监控，只需杀死当前 uvicorn 进程即可：
```bash
pkill -f 'uvicorn.*app:app.*3080'
# 等待 3 秒后查看新进程是否拉起：
ps -ef | grep 'uvicorn.*app:app' | grep -v grep
```

### 3. 快速健康检查与服务验证
```bash
# 1. 检查 ProGrok 注册服务与本地 Solver 就绪状态
curl -s http://127.0.0.1:3080/api/health

# 2. 检查 20172 专线池当前最优出海延迟
curl -s http://127.0.0.1:9090/proxies/ProGrok-Auto

# 3. 检查生产 3001 / 3002 翻译服务健康状态（严禁受影响）
curl -s http://127.0.0.1:3001/v1/models | head -c 100
curl -s http://127.0.0.1:3002/v1/models | head -c 100
```

### 4. 查看落盘账号与入库统计
```bash
# 查看最新生成的账号 JSON 文件
ls -lt /workspace/progrok/runtime/data/accounts/ | head -n 10

# 统计当前总注册入库账号数
ls -1 /workspace/progrok/runtime/data/accounts/ | wc -l
```

---

## 六、 常见问题排除指南 (FAQ)

### Q1: 遇到 `400 Expecting value: line 1 column 1 (char 0)` 报错？
- **根因**：使用了已失效的邮箱 API 地址（如 `maliapi.215.im`）返回了 HTML 重定向，导致 JSON 解析器崩溃。
- **解决**：检查配置中的 `mail_base_url` 是否为 `https://local-mailbox.your-worker.workers.dev`。当前系统已增加防御性纠偏。

### Q2: 提示 `waiting_email` 超时未收到验证码？
- **排查步骤**：
  1. 检查是否在代码中拦截修改了 `castleRequestToken`（若有，必须撤销拦截）；
  2. 检查所用域名是否有 Cloudflare MX 路由（目前仅限 `rejected-example.online` 与 `example1.space`）；
  3. 执行连通性测试：
     `curl -s https://local-mailbox.your-worker.workers.dev/open_api/settings`

### Q3: 提示 `account:email-signup-unavailable`？
- **根因**：当前出口节点 IP 被 xAI 临时频控或标记。
- **解决**：系统已内置自动刷新页面轮换节点机制。若仍频繁发生，可通过 Mihomo API 检查 20172 专线池节点质量或触发节点重测。

### Q4: 提示 `waiting_solver` 超时？
- **排查**：检查 5072 端口是否存活：`curl http://127.0.0.1:5072/`。
- **解决**：若 5072 僵死，执行 `pkill -f 'api_solver.py'`，supervisor 会在 5 秒内自动重启 solver。
