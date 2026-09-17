# 3-translation-validator (API Quality Guard & Practical Validator)

> 专门针对网络小说（韩翻中、日翻中等）长文本切块翻译设计的生产级质量内审与修复代理网关。

---

## 核心定位与痛点解决

在利用大语言模型（如 Grok-3 / Grok-2 / Claude / GPT-4o）进行批量网络小说机翻时，客户端（如 NovelPie、Cherry Studio 等）通常会将章节按行切分成 JSON 字典发送给模型。然而原生 LLM 在长文本翻译中存在三大致命痛点：

1. **幻觉与漏翻 (Drop / Repetition)**：
   - 遇到生僻词或长句时，模型可能直接复读韩文原文，或漏掉整行。普通网关无法感知，直接将带有大段韩文的译文返回给客户端，导致整章报废。
2. **结构破坏 (JSON Corruption)**：
   - 模型经常吞掉空行（如 `{"5": ""}`），导致行号错位；或者输出非法 Markdown 代码块包裹，造成客户端 JSON 解析失败。
3. **恶意/故障文本误杀 (Glitch Text False Positive)**：
   - 网络小说中常出现克苏鲁式乱码、拟声词、破损对话（如 `(벨겟굶눋뜩뢰겆뀐걸흐흐흐)` 或 `“이.`0ㅇ거 3$<i>ㅂ</i>...”`）。普通质检程序会误将其当成“未翻译韩文”而反复重试直至 502 报错超时。

**`translation-validator` 作为反代前端的透明中间件，在毫秒级内完成自动清洗、结构校验、乱码赦免、轻微残留放行与模型主备降级重试。**

---

## 核心特性

- **三级实用质检流水线 (Practical 3-Tier Validator)**：
  - **PASS**：完美翻译直接放行。
  - **WARN**：轻度韩文残留（如单一尊称 `오빠`）、合法英文词汇（如游戏术语 `status`、`HP`）放行并记录遥测，不阻断请求。
  - **HARD FAIL**：韩文原文整行复读、大段未翻译韩文、严重丢失行号则立即拦截并触发降级重试。
- **OPAQUE_LITERAL 确定性乱码分类器**：
  - 基于音位学（Phonotactics）与结构特征，精准识别网络小说中的拟声词、克苏鲁乱码、故障对话与符号穿插文本，False Positive = 0，彻底根除 502 超时死循环。
- **neutral_v1 高质量 Prompt 动态注入**：
  - 自动识别客户端的劣质提示词，动态替换为经过上千章小说验证的 `neutral_v1` 翻译工作流指令，保障术语表、句式与标点规范。
- **智能就地热修复 (Safe Local Repair)**：
  - 客户端被吞掉的纯空行键值自动原位补齐，无需耗费额外 Token 重新请求上游。
- **多级模型主备故障转移 (Dual-Model Fallback)**：
  - 首选高精度推理模型（如 `grok-4.20-0309-reasoning`），若触发 HARD FAIL 或上游 5xx，毫秒级无缝降级至兜底模型（如 `grok-3`）。
- **零外部运行时依赖 (Zero Pip Dependencies)**：
  - 基于 Python 3.10+ 标准库（`http.server`, `urllib`, `re`, `json`）实现高并发异步 IO 与审计日志轮转，开箱即用。
- **可视化运维看板**：
  - 内置 Web 监控页面（访问 `http://localhost:3002/audit`），实时查看请求耗时、成功率、质检阻断原因、警告统计与主备降级详情。

---

## 架构示意

```
+------------------+         +-------------------------------+         +-----------------------+
| Translation App  |         | 3-translation-validator       |         | 2-grok2api            |
| (NovelPie / etc) | ------> | (Port 3002)                   | ------> | (Port 3001)           |
+------------------+         |                               |         |                       |
                             | 1. Prompt 规范化注入          |         | 逆向 / 协议反向代理   |
                             | 2. 上游请求转发               |         +-----------------------+
                             | 3. JSON 修复与键值补齐        |                     |
                             | 4. OPAQUE_LITERAL 乱码判定    |                     v
                             | 5. 3-Tier 质量审计            |         +-----------------------+
                             | 6. 自动降级与遥测看板         |         | Upstream Grok Service |
                             +-------------------------------+         +-----------------------+
```

---

## 环境变量配置

支持通过环境变量或根目录 `.env` 文件进行配置：

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LISTEN_HOST` | `0.0.0.0` | 代理监听地址 |
| `PORT` / `LISTEN_PORT` | `3002` | 代理监听端口 |
| `UPSTREAM_HOST` | `127.0.0.1` | 上游反代服务地址（如 grok2api） |
| `UPSTREAM_PORT` | `3001` | 上游反代服务端口 |
| `UPSTREAM_AUTH_TOKEN` | *(空)* | 上游网关的 API Key（若有） |
| `CONFIG_FILE` | `./config/translation_profiles.json` | 租户/客户端多 Profile 配置文件路径 |
| `LOG_DIR` | `./logs` | 请求与遥测日志存储目录 |
| `ENABLE_QUALITY_PROMPT_OVERRIDE` | `true` | 是否启用 neutral_v1 提示词动态注入 |
| `ENABLE_PHONETIC_TRANSLITERATION_MATCHER` | `true` | 是否启用音位匹配识别音译英文 |

---

## 快速上手

### 方式一：直接运行 (Python 3.10+)

```bash
# 1. 复制配置文件示例
cp config.example.json config/translation_profiles.json

# 2. 启动服务
python grok_audit_proxy.py
```

### 方式二：Docker 容器化部署

```bash
# 构建镜像
docker build -t grok-translation-validator .

# 启动容器
docker run -d \
  --name grok-validator \
  -p 3002:3002 \
  -e UPSTREAM_HOST="host.docker.internal" \
  -e UPSTREAM_PORT="3001" \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  grok-translation-validator
```

---

## 单元测试与验证

本项目内置完整的规则与乱码分类器单测套件：

```bash
# 运行 OPAQUE_LITERAL 乱码与故障文本分类测试
python tests/test_opaque_classifier.py

# 运行 3-Tier 实用质检规则集成测试（包含韩语复读拦截、警告放行、空键补齐等）
python tests/test_validator_rules.py
```

---

## 核心技术文档

- [Practical Validator 质检规范与指标基线](docs/PRACTICAL_VALIDATOR.md)
- [OPAQUE_LITERAL 乱码分类器原理与规则指南](docs/OPAQUE_LITERAL_GUIDE.md)
- [neutral_v1 小说翻译专用系统提示词规范](docs/PROMPT_TEMPLATE.md)
