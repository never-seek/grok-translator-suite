# 2-grok2api (High-Concurrency Grok Reverse Proxy Gateway)

> 高并发 Grok 多账号反代与负载均衡网关，将 xAI 官方网页端/开发者控制台会话转换为标准 OpenAI 兼容接口。

---

## 核心功能

- **OpenAI 兼容协议**：
  - 提供标准的 `/v1/chat/completions`、`/v1/models`、`/v1/embeddings` 等 API 规范，无缝对接各类翻译器、IDE 插件与 Web 客户端。
- **多账号池轮询与负载均衡 (Account Pooling)**：
  - 支持批量挂载普通账号、Super 账号与开发者 Console 凭据。
  - 自动跟踪每个账号的速率限制（Rate Limits）与配额窗口，智能轮询与冷却隔离。
- **全模型映射与路由支持**：
  - 支持 `grok-3`、`grok-4.20-0309-reasoning`、`grok-2` 等最新模型别名映射与能力发现。
- **自动会话维持与凭证续期**：
  - 自动刷新 SSO / OAuth Token 与 Cloudflare Cookie，保障长时间大批量小说机翻不断连。
- **出口节点代理支持 (Egress Proxy Nodes)**：
  - 支持为不同账号配置独立的出站代理节点，避免同 IP 多账号并发触发风控。

---

## 目录结构

```
2-grok2api/
├── config.example.yaml        # 配置文件模板（请复制为 config.yaml）
├── docker-compose.yml         # 独立运行编排配置
├── init_db.sql                # 纯净 SQLite 数据库全量结构定义
├── scripts/
│   └── cleanup-db-safe.sh     # 生产环境安全日志轮转与数据库压缩脚本
└── README.md                  # 本文档
```

---

## 快速上手

### 1. 配置准备

```bash
# 复制配置文件模板
cp config.example.yaml config.yaml

# 生成 64 位十六进制 JWT 密钥
openssl rand -hex 32

# 生成 32 字节 Base64 凭据加密密钥
openssl rand -base64 32
```

编辑 `config.yaml`：
- 将生成的密钥填入 `secrets.jwtSecret` 和 `secrets.credentialEncryptionKey`。
- 修改 `bootstrapAdmin.password` 为强密码。
- 确保 `database.sqlite.path` 设为 `./data/backend.db`。

### 2. 启动服务 (Docker)

```bash
docker compose up -d
```

服务默认在 `http://localhost:3001` 启动。首次启动访问管理面板：
- 默认后台：`http://localhost:3001`
- 初始账号：`admin`
- 初始密码：你在 `config.yaml` 中设置的密码

---

## 运维与生产加固指南

### 1. 数据库日志安全清理 (`cleanup-db-safe.sh`)

高并发长文本翻译时，`request_audits` 和 `request_audit_attempts` 表每天会产生数万行审计日志。**务必注意：**

> [!CAUTION]
> SQLite 默认未开启外键级联检查。若直接删除 `request_audits`，会导致 `request_audit_attempts` 产生大量孤儿数据，进而引发管理后台 502 报错。

本项目提供了经过严格生产检验的 `scripts/cleanup-db-safe.sh`：

```bash
# 赋予执行权限
chmod +x scripts/cleanup-db-safe.sh

# 手动清理 3 天前的历史审计并整理磁盘空间
./scripts/cleanup-db-safe.sh ./data/backend.db

# 推荐加入系统 cron 每小时或每天执行：
# 0 3 * * * /path/to/2-grok2api/scripts/cleanup-db-safe.sh /path/to/2-grok2api/data/backend.db >/dev/null 2>&1
```

### 2. 联动 `3-translation-validator`

在配套全栈架构中，`grok2api` 部署在内网端口（如 `3001`），客户端请求优先打到 `translation-validator`（端口 `3002`），由质检中间件完成 Prompt 注入与质量拦截后，再透明转发给本网关。
