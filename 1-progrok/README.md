# 1-progrok (Automated Protocol Registration & Turnstile Solver)

> 全自动 Grok 批量协议注册系统，集成本地 Camoufox Turnstile 人机验证破解器与 Grok2API 一键账号池同步。

---

## 核心功能与架构

1. **纯协议极速注册 (Protocol Registration)**：
   - 绕过厚重的全流程网页模拟，直接调用 xAI 注册底层协议接口，注册耗时低至毫秒级，单机支持高并发批量产号。
2. **本地免打码验证码破解器 (`turnstile-solver`)**：
   - 基于深度反指纹浏览器 **Camoufox** 实现 Cloudflare Turnstile 验证码的高成功率本地自动破解。
   - 零第三方打码平台消费，随启随用，支持多线程并发求解。
3. **临时邮箱与验证码自动提取**：
   - 支持 Cloudflare Workers 极简自建临时邮箱后端、自定义邮件转发或三方临时邮箱协议，自动监听并提取邮件验证码。
4. **全自动无缝导入 Grok2API**：
   - 账号注册成功后，自动完成模型健康检查（Probe），并将生成的 Token 与会话凭据直接通过 API 导入 `grok2api` 账号池，无需人工导出复制。
5. **现代化 Web 控制台**：
   - 访问 `http://localhost:3080` 即可实时查看注册进度、成功率、代理池存活健康度及一键启停任务。

---

## 目录结构

```
1-progrok/
├── backend/                   # FastAPI 后端服务与协议流水线
│   ├── app.py                 # API 入口与路由
│   ├── account_pipeline.py    # 账号生成与导入流水线
│   ├── grok_build_adapter.py  # xAI 注册底层协议适配
│   ├── moemail.py             # 临时邮箱协议对接
│   ├── proxy_pool.py          # 代理轮询与健康检测
│   └── requirements.txt       # 后端 Python 依赖
├── turnstile-solver/          # 本地 Turnstile 人机验证求解器
│   ├── api_solver.py          # 求解器 HTTP API
│   ├── browser_configs.py     # Camoufox 反指纹注入配置
│   ├── Dockerfile             # 求解器容器化构建
│   └── run_solver.sh          # 求解器启动脚本
├── vendor/                    # 依赖组件库
│   ├── grok-build-auth/       # xAI 核心底层协议客户端 (xconsole_client)
│   └── turnstile-solver/      # 本地求解器组件
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

### 1. 环境准备与配置

```bash
# 复制配置文件
cp config.example.json config/config.json
```

修改 `config/config.json` 中的关键配置：
- `mail_base_url` / `mail_domain`：配置你的临时邮箱接收服务。
- `grok2api_base_url`：填写配套的 Grok2API 服务地址（如 `http://127.0.0.1:3001`）。
- `grok2api_admin_password`：填写 Grok2API 的管理员密码以启用自动同步。
- `proxy`：若服务器 IP 被 xAI 屏蔽，可在此配置住宅代理或上游代理池。

### 2. 启动本地验证码求解器 (`turnstile-solver`)

```bash
cd turnstile-solver

# 安装依赖
pip install -r requirements.txt
python -m camoufox fetch

# 启动本地求解器服务（监听 5072 端口）
python api_solver.py --browser_type camoufox --thread 2 --host 0.0.0.0 --port 5072
```

### 3. 启动 ProGrok 控制后台

```bash
cd ../backend

# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务
python -m uvicorn app:app --host 0.0.0.0 --port 3080
```

打开浏览器访问 `http://localhost:3080` 进入可视化管理界面，点击「开始注册」即可进行自动化产号与同步。

---

## Windows 环境快捷运行

在 Windows 系统上，可以直接双击运行：
- **首次启动**：双击 `install_and_start.cmd`（自动检测 Python 环境、安装依赖并拉起所有后台组件）。
- **日常启停**：使用 `start.cmd` 和 `stop.cmd`。
