# Grok Translator Suite 🚀

> **专为长篇网络小说（韩翻中、日翻中）高强度机翻量身打造的全栈 Grok 基础设施套件。**  
> 涵盖 **底层极速协议注册**、**高并发反代负载均衡** 与 **独创三级生产级质检中间件**，提供工业级稳定输出。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker%20Compose-Ready-blue.svg)](docker-compose.yml)
[![Validation Baseline](https://img.shields.io/badge/Production%20Baseline-Frozen%20v1-success.svg)](3-translation-validator/docs/PRACTICAL_VALIDATOR.md)

---

## 为什么需要这套系统？

利用最新顶级大模型（如 Grok-3、Grok-4.20-reasoning）进行网络小说整本百万字批量机翻时，开发者与读者通常会遭遇三大致命瓶颈：

| 致命痛点 | 原生 LLM / 普通反代表现 | Grok Translator Suite 解决方案 |
| :--- | :--- | :--- |
| **生僻字原文复读** | 模型遇到生僻韩文/复杂长句直接原样吐出韩文，污染整章文本 | **3-Tier Practical Validator**：毫秒级拦截 `raw_repetition_exact` 并自动触发兜底重试 |
| **格式与空行错位** | 模型经常吞掉空行或吐出 Markdown 标记，破坏客户端按行切片结构 | **Safe Local Repair**：就地原位热补齐空行键值，零额外 Token 损耗 |
| **故障/克苏鲁文本误杀** | 小说中的拟声词、破损对话、克苏鲁乱码被普通质检当成未翻译报错 502 | **OPAQUE_LITERAL 确定性分类器**：音位学分析，精准赦免放行，False Positive = 0 |
| **账号消耗与额度受限** | 翻译上千万字需要大量账号轮换与高昂验证码打码费 | **ProGrok + 本地 Camoufox 求解器**：全协议极速产号，0 打码费用，自动同步入池 |

---

## 总体架构与数据流向

```mermaid
flowchart TD
    subgraph Client ["客户端生态"]
        NP["NovelPie (小说派)"]
        CS["Cherry Studio / NextChat"]
        SDK["OpenAI SDK / Python Scripts"]
    end

    subgraph Suite ["Grok Translator Suite"]
        subgraph Mod3 ["3-translation-validator (端口 3002)"]
            P_INJ["neutral_v1 提示词注入"]
            AUDIT["3-Tier 质量审计 (PASS / WARN / HARD FAIL)"]
            REPAIR["就地空键修复 (Safe Local Repair)"]
            OPAQUE["OPAQUE_LITERAL 乱码分类器"]
            FB["主备模型无缝降级 (Dual-Model Fallback)"]
        end

        subgraph Mod2 ["2-grok2api (端口 3001)"]
            GATEWAY["OpenAI 兼容 API 网关"]
            POOL["账号池负载均衡 & 配额轮询"]
            ROTATE["Session / Cloudflare Cookie 自动保活"]
            EGRESS["多出口代理路由 (Egress Nodes)"]
        end

        subgraph Mod1 ["1-progrok (端口 3080 / 5072)"]
            REG["底层纯协议极速注册引擎"]
            SOLVER["本地 Camoufox Turnstile 破解器 (:5072)"]
            AUTO_IMP["一键健康探测并自动导入 Grok2API"]
        end
    end

    subgraph Upstream ["xAI 上游服务"]
        XAI["xAI Official API / Web / Console"]
    end

    Client -->|1. 翻译请求 (JSON Dict)| Mod3
    P_INJ --> GATEWAY
    GATEWAY -->|2. 负载均衡转发| XAI
    XAI -->|3. 原始译文流| GATEWAY
    GATEWAY -->|4. 响应回传| Mod3
    Mod3 -->|5. 质检/修复/放行| Client
    Mod1 -.->|全自动产号 & 凭据同步| Mod2
```

---

## 三大核心模块简介

### 1. [ProGrok 协议注册与本地打码器 (`1-progrok/`)](1-progrok/README.md)
- **底层纯协议注册**：无需笨重全流程浏览器模拟，极速调用 xAI 注册协议。
- **本地免打码验证码破解器**：基于 **Camoufox** 反指纹引擎，实现 Cloudflare Turnstile 验证码的高并发本地攻破，告别三方打码平台费用。
- **自动化闭环**：临时邮箱接收 -> 协议注册 -> 模型可用性探测 (Probe) -> 自动注入 `grok2api` 账号池。

### 2. [Grok2API 反代与账号池网关 (`2-grok2api/`)](2-grok2api/README.md)
- **标准 OpenAI 接口**：对外暴露标准的 `/v1/chat/completions` 与 `/v1/models`，完美兼容一切下游生态。
- **多账号负载均衡**：自动维护各账号速率限制、请求冷却、剩余配额与出站代理节点。
- **生产级安全清理脚本**：内附 `scripts/cleanup-db-safe.sh`，强制外键检查级联清理历史审计，避免孤儿数据引发后台 502。

### 3. [API 内审与 Practical Validator (`3-translation-validator/`)](3-translation-validator/README.md)
- **三级实用质检流水线**：
  - `PASS`：合规文本直接返回。
  - `WARN`：轻度韩文尊称残留（如 `오빠`）、合法游戏界面英文（如 `status`、`HP`）放行并记录遥测，不卡单、不打断翻译。
  - `HARD FAIL`：整句复读韩文原文、大段未翻译、缺失行号则立即拦截并触发降级重试。
- **OPAQUE_LITERAL 确定性乱码分类器**：利用音位学规则甄别拟声词、破损对话与克苏鲁乱码，彻底解决小说故障文本导致重试超时 502 的痛点。
- **neutral_v1 翻译提示词动态注入**：上千章小说实战沉淀的 System Prompt，规范标点、术语表继承与行号对齐。
- **实时运维监控面板**：自带 Web 看板（`http://localhost:3002/audit`），实时观测质检通过率与模型降级情况。

---

## 快速上手 (Linux / VPS 一键部署)

适合拥有自己服务器（Ubuntu / Debian / CentOS / AlmaLinux 等）的用户，全程仅需执行一条命令：

### 1. 检出项目

```bash
git clone https://github.com/your-username/grok-translator-suite.git
cd grok-translator-suite
```

### 2. 执行一键部署

```bash
chmod +x deploy.sh manage.sh
./deploy.sh
```

脚本将自动执行以下全流程：
1. 检测并自动就绪 Docker 及 Docker Compose 环境；
2. 自动生成高强度独立随机密钥（JWT Secret、凭据加密 Key、随机管理员密码，做到**全脱敏且千人千密**）；
3. 预载入纯净 SQLite 数据库表结构；
4. 容器化一键构建并拉起全套三个模块服务；
5. 在终端打印你的公网服务访问入口、管理凭据与翻译软件（NovelPie 等）的配置范例。

---

## 日常运维与监控 (`./manage.sh`)

项目提供了极其简便的运维管理脚本：

```bash
# 查看全套服务运行状态与健康度
./manage.sh status

# 查看实时聚合日志（或指定服务名如 ./manage.sh logs translation-validator）
./manage.sh logs

# 重启全部服务
./manage.sh restart

# 安全清理历史审计与碎片，释放磁盘空间（内置外键保护，防 502）
./manage.sh cleanup

# 一键拉取更新并重新加载
./manage.sh update
```

启动完成后，系统各服务就绪：
- **翻译客户端对接地址**：`http://你的服务器IP:3002/v1`（质检代理前端）
- **质检监控看板**：`http://你的服务器IP:3002/audit`
- **Grok2API 管理后台**：`http://你的服务器IP:3001`
- **ProGrok 注册控制台**：`http://你的服务器IP:3080`

---

## 客户端配置指南 (以 NovelPie 为例)

在 **NovelPie (小说派)** 或 **Cherry Studio** 中，添加自定义 OpenAI 兼容提供商：

- **API Base URL**：`http://你的服务器IP:3002/v1`
- **API Key**：`sk-grok-translator`（或任意字符串，亦可在 `3-translation-validator` 的配置文件中开启租户 Profile 多 Key 校验）
- **主选模型 (Primary)**：`grok-4.20-0309-reasoning`（高精度推理首选）
- **兜底模型 (Fallback)**：`grok-3`（质检代理遇故障自动降级）

---

## 生产运行基线 (Production Freeze Baseline)

本项目搭载的 Practical 3-Tier Validator 判定规则已完成 1000+ 章节真实自然韩语长篇小说翻译的长期稳定性封板验收：

```
Window Observation Chunks: 1051
Overall HTTP 200 Success Rate: 98.29%
First-pass Reasoning Release: 96.9%
OPAQUE_LITERAL False Positive: 0
Zero Semantic On-path Latency Overhead
```

详见技术文档：[Practical Validator 质检标准与运行基线](3-translation-validator/docs/PRACTICAL_VALIDATOR.md)。

---

## 贡献与授权

- 本项目采用 [MIT License](LICENSE) 开源协议。
- 欢迎提交 Issue 与 Pull Request 共同改进翻译质量判定规则与反代适配支持！
