# Grok Translator Suite 🚀

> **Production-grade full-stack Grok infrastructure specifically architected for long-form web novel (Korean/Japanese to Chinese/English) batch machine translation.**  
> Combines **high-speed protocol registration**, **high-concurrency reverse proxy account pooling**, and a **proprietary 3-tier practical quality guard & validator**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker%20Compose-Ready-blue.svg)](docker-compose.yml)
[![Validation Baseline](https://img.shields.io/badge/Production%20Baseline-Frozen%20v1-success.svg)](3-translation-validator/docs/PRACTICAL_VALIDATOR.md)

---

## Why Grok Translator Suite?

When translating millions of words of web novels using cutting-edge LLMs (such as Grok-3 and Grok-4.20-reasoning), translation pipelines inevitably encounter critical roadblocks:

| Pain Point | Standard Proxy / Raw LLM Behavior | Grok Translator Suite Solution |
| :--- | :--- | :--- |
| **Source Repetition** | LLMs occasionally repeat raw Korean sentences verbatim, ruining entire chapters | **3-Tier Practical Validator**: Intercepts `raw_repetition_exact` in milliseconds and initiates automatic fallback retry |
| **Dropped Lines & JSON Corruption** | LLMs drop empty lines (e.g. `{"5": ""}`) or wrap outputs in markdown fences, breaking client line alignment | **Safe Local Repair**: Instantly restores missing empty keys in-place with zero extra token overhead |
| **Glitch / Cosmic Horror False Failures** | Sound effects, corrupted dialogue, and intentional glitch text are misclassified as untranslated text, resulting in 502 retry loops | **OPAQUE_LITERAL Deterministic Classifier**: Uses phonotactic analysis to pardon novel glitch text with zero false positives |
| **Account Churn & CAPTCHA Costs** | High-volume translation consumes numerous accounts and runs up third-party CAPTCHA solving bills | **ProGrok + Local Camoufox Solver**: 100% protocol-based batch account creation with free local Turnstile solving and auto-sync |

---

## System Architecture & Data Flow

```mermaid
flowchart TD
    subgraph Client ["Translation Clients"]
        NP["NovelPie"]
        CS["Cherry Studio / NextChat"]
        SDK["OpenAI SDK / Scripts"]
    end

    subgraph Suite ["Grok Translator Suite"]
        subgraph Mod3 ["3-translation-validator (Port 3002)"]
            P_INJ["neutral_v1 Prompt Injection"]
            AUDIT["3-Tier Practical Quality Audit (PASS / WARN / HARD FAIL)"]
            REPAIR["Safe Local Repair (In-place key restoration)"]
            OPAQUE["OPAQUE_LITERAL Glitch Text Classifier"]
            FB["Dual-Model Seamless Fallback"]
        end

        subgraph Mod2 ["2-grok2api (Port 3001)"]
            GATEWAY["OpenAI-compatible API Gateway"]
            POOL["Account Pooling & Quota Balancing"]
            ROTATE["Session & Cloudflare Cookie Keepalive"]
            EGRESS["Egress Proxy Node Router"]
        end

        subgraph Mod1 ["1-progrok (Port 3080 / 5072)"]
            REG["Pure Protocol Registration Engine"]
            SOLVER["Local Camoufox Turnstile Solver (:5072)"]
            AUTO_IMP["Automated Model Probe & Account Sync"]
        end
    end

    subgraph Upstream ["xAI Upstream"]
        XAI["xAI Official API / Web / Console"]
    end

    Client -->|1. Translation Request (JSON Dict)| Mod3
    P_INJ --> GATEWAY
    GATEWAY -->|2. Balanced Dispatch| XAI
    XAI -->|3. Raw Stream Response| GATEWAY
    GATEWAY -->|4. Forward Response| Mod3
    Mod3 -->|5. Validate / Repair / Pass| Client
    Mod1 -.->|Auto Register & Sync Credentials| Mod2
```

---

## Core Modules Overview

### 1. [ProGrok Protocol Registration & Local Solver (`1-progrok/`)](1-progrok/README.md)
- **Pure Protocol-Based Registration**: Extremely fast account creation bypassing heavyweight browser automation.
- **Local Turnstile CAPTCHA Solver**: Powered by **Camoufox** anti-fingerprinting browser to solve Cloudflare Turnstile locally with 0 API fees.
- **Automated Lifecycle**: Temp email reception -> Protocol registration -> Model health probe -> Instant auto-import into Grok2API account pool.

### 2. [Grok2API Reverse Proxy Gateway (`2-grok2api/`)](2-grok2api/README.md)
- **OpenAI-Compatible Endpoints**: Exposes standard `/v1/chat/completions` and `/v1/models` to integrate seamlessly with any translation tool.
- **Multi-Account Load Balancing**: Manages concurrency limits, cooldowns, remaining quotas, and egress proxy nodes.
- **Hardened Database Maintenance**: Includes `scripts/cleanup-db-safe.sh` with foreign-key cascade enforcement to prevent SQLite database corruption and 502 errors.

### 3. [Translation Validator Middleware (`3-translation-validator/`)](3-translation-validator/README.md)
- **3-Tier Practical Validator**:
  - `PASS`: Clean translation immediately approved.
  - `WARN`: Minor residual honorifics (e.g. `오빠`) or legitimate game terms (`status`, `HP`) logged and passed without interrupting the translation pipeline.
  - `HARD FAIL`: Verbatim Korean repetition, massive untranslated blocks, or missing line keys intercepted and rerouted.
- **OPAQUE_LITERAL Deterministic Classifier**: Phonotactic analysis of onomatopoeia, broken speech, and corrupted dialogue, eliminating 502 retry timeouts.
- **neutral_v1 Prompt Injection**: Novel translation prompt refined over thousands of chapters, enforcing punctuation, glossary retention, and line alignment.
- **Live Audit Dashboard**: Embedded web monitoring UI (`http://localhost:3002/audit`) for inspecting latency, pass rates, and model fallback stats.

---

## Quickstart (One-Click Linux / VPS Deployment)

Tailored for users with their own Linux VPS (Ubuntu / Debian / CentOS / AlmaLinux), requiring only one command to deploy:

### 1. Clone Repository

```bash
git clone https://github.com/your-username/grok-translator-suite.git
cd grok-translator-suite
```

### 2. Execute One-Click Deployment

```bash
chmod +x deploy.sh manage.sh
./deploy.sh
```

The script automatically handles:
1. Environment verification (installs Docker and Compose if missing);
2. Cryptographically secure random secret generation (JWT Secret, encryption key, admin password — 100% sanitized with unique keys per deployment);
3. SQLite database schema initialization;
4. Multi-container build and launch via Docker Compose;
5. Summary output with public IP endpoints, credentials, and translation client setup instructions.

---

## Operations & Management (`./manage.sh`)

Manage the running suite with the bundled management CLI:

```bash
# Check running status and health of all modules
./manage.sh status

# View live aggregate logs (or pass module name, e.g. ./manage.sh logs translation-validator)
./manage.sh logs

# Restart all services
./manage.sh restart

# Safely purge historical audits and reclaim disk space (foreign-key protected)
./manage.sh cleanup

# Pull latest images and reload
./manage.sh update
```

Once started, the services will be reachable at:
- **Client API Endpoint**: `http://YOUR_SERVER_IP:3002/v1` (Translation Validator Proxy)
- **Audit Web Dashboard**: `http://YOUR_SERVER_IP:3002/audit`
- **Grok2API Admin Panel**: `http://YOUR_SERVER_IP:3001`
- **ProGrok Web Console**: `http://YOUR_SERVER_IP:3080`

---

## Client Setup (e.g., NovelPie)

Configure your custom OpenAI-compatible provider in **NovelPie** or **Cherry Studio**:

- **API Base URL**: `http://your-server-ip:3002/v1`
- **API Key**: `sk-grok-translator` (or any string; profile-based multi-key authentication can be configured in `3-translation-validator`)
- **Primary Model**: `grok-4.20-0309-reasoning` (recommended for superior translation nuance)
- **Fallback Model**: `grok-3` (automatically engaged on HARD FAIL or upstream timeout)

---

## Production Stability Baseline

The Practical 3-Tier Validator has completed long-term production observation across 1000+ real Korean web novel chapters:

```
Window Observation Chunks: 1051
Overall HTTP 200 Success Rate: 98.29%
First-pass Reasoning Release: 96.9%
OPAQUE_LITERAL False Positive: 0
Zero Semantic On-path Latency Overhead
```

See the full specification in [Practical Validator Specification](3-translation-validator/docs/PRACTICAL_VALIDATOR.md).

---

## Contributing & License

- Licensed under the [MIT License](LICENSE).
- Issues and Pull Requests are welcome to further improve validator rules and reverse proxy compatibility!
