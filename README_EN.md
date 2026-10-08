# Grok Translator Suite 🚀

> **Production-grade full-stack Grok infrastructure specifically architected for high-throughput batch web novel machine translation (Korean/Japanese to Chinese/English).**  
> Features **Camoufox local authentic-fingerprint Turnstile solving**, **12+ domain dynamic cooldown email routing**, **high-concurrency reverse proxy pooling**, and a **proprietary 3-tier practical quality guard middleware**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker%20Compose-Ready-blue.svg)](docker-compose.yml)
[![Validation Baseline](https://img.shields.io/badge/Production%20Baseline-Frozen%20v1-success.svg)](3-translation-validator/docs/PRACTICAL_VALIDATOR.md)
[![D1 Health](https://img.shields.io/badge/Cloudflare%20D1-Healthy%200.86%25-brightgreen.svg)](#3-database--storage-safety-cloudflare-d1-sqlite)

---

## Table of Contents

- [Why Grok Translator Suite?](#why-grok-translator-suite)
- [System Architecture & Full Pipeline Data Flow](#system-architecture--full-pipeline-data-flow)
- [Registration Evolution: Pure Protocol to Camoufox Authentic Fingerprint Solving](#registration-evolution-pure-protocol-to-camoufox-authentic-fingerprint-solving)
- [Infrastructure Deep Dive](#infrastructure-deep-dive)
  - [1. Proxy & Egress Routing (Mihomo Proxy Pool)](#1-proxy--egress-routing-mihomo-proxy-pool)
  - [2. Domain & Email Systems (Cloudflare Email Routing)](#2-domain--email-systems-cloudflare-email-routing)
  - [3. Database & Storage Safety (Cloudflare D1 SQLite)](#3-database--storage-safety-cloudflare-d1-sqlite)
- [Quantified Post-Mortem: 3,577 Production Failures Analyzed](#quantified-post-mortem-3577-production-failures-analyzed)
- [Model Selection Criteria & Degradation Warnings](#model-selection-criteria--degradation-warnings)
  - [Golden Standard: Why Novel Translation is Strictly Locked to grok-4.2](#golden-standard-why-novel-translation-is-strictly-locked-to-grok-42)
  - [Pitfalls & Warnings for Other Models (grok-4.6 / grok-3 / grok-2)](#pitfalls--warnings-for-other-models-grok-46--grok-3--grok-2)
- [Modules Overview](#modules-overview)
- [Quickstart (Linux / VPS One-Click Deployment)](#quickstart-linux--vps-one-click-deployment)
- [Operations & Management (`./manage.sh`)](#operations--management-managesh)
- [Client Integration Guide (NovelPie / Cherry Studio)](#client-integration-guide-novelpie--cherry-studio)
- [Production Freeze Baseline](#production-freeze-baseline)

---

## Why Grok Translator Suite?

When translating millions of words of web novels using raw LLMs and basic reverse proxies, developers and readers frequently encounter critical roadblocks:

| Pain Point | Standard Proxy / Raw LLM Behavior | Grok Translator Suite Solution |
| :--- | :--- | :--- |
| **Source Repetition** | LLMs repeat raw Korean sentences verbatim when facing rare idioms, destroying chapters | **3-Tier Practical Validator**: Millisecond interception of `raw_repetition_exact` with automatic retry |
| **Dropped Lines & JSON Corruption** | LLMs omit blank lines (e.g. `{"5": ""}`) or output markdown fences, misaligning line slices | **Safe Local Repair**: Restores missing empty keys in-place with zero extra token cost |
| **Glitch / Cosmic Horror False Failures** | SFX, broken dialogue, and glitch text trigger untranslated errors, causing 502 retry loops | **OPAQUE_LITERAL Deterministic Classifier**: Phonotactic analysis grants automatic pardons, False Positives = 0 |
| **CAPTCHA Costs & Failures** | 3rd-party CAPTCHA APIs fail against Cloudflare's new Turnstile fingerprint checks and cost a fortune | **Local Camoufox Fingerprint Solver**: Zero solver fees, 100% authentic browser emulation with >99% pass rate |
| **Domain Rate Limits & Pipeline Freezes** | Rapid single-domain signups trigger `email-signup-unavailable`, freezing pool generation | **12+ Domain Pool Rotation & Staggered Scheduling**: Evenly spreads hourly quotas for uninterrupted operation |

---

## System Architecture & Full Pipeline Data Flow

```mermaid
flowchart TD
    subgraph Client ["Client Ecosystem"]
        NP["NovelPie"]
        CS["Cherry Studio / NextChat"]
        SDK["OpenAI SDK / Python Scripts"]
    end

    subgraph Suite ["Grok Translator Suite"]
        subgraph Mod3 ["3-translation-validator (Port 3002)"]
            P_INJ["neutral_v1 Prompt Injection"]
            AUDIT["3-Tier Quality Audit (PASS / WARN / HARD FAIL)"]
            REPAIR["Safe Local Repair (In-place key restoration)"]
            OPAQUE["OPAQUE_LITERAL Glitch Classifier"]
            FB["Dual-Model Seamless Fallback"]
        end

        subgraph Mod2 ["2-grok2api (Port 3001)"]
            GATEWAY["OpenAI-Compatible API Gateway"]
            POOL["Account Pool Load Balancer (20,000+ Accounts)"]
            ROTATE["Session & Cloudflare Cookie Keepalive"]
            EGRESS["Multi-Egress Proxy Routing (Mihomo)"]
        end

        subgraph Mod1 ["1-progrok (Port 3080 / 5072)"]
            REG["Registration Dispatch Engine (:3080)"]
            SOLVER["Local Camoufox Turnstile Solver (:5072)"]
            MAIL_POLL["Cloudflare D1 Mail Poller"]
            AUTO_IMP["Model Health Probe & Auto-Import"]
        end

        subgraph Infra ["Infrastructure & Network Pools"]
            PROXY_POOL["Mihomo Proxy Pool (:20172) - Dynamic Node Rotation"]
            CF_DOMAINS["12+ Domain Pool (Cloudflare Email Routing)"]
            CF_D1["Cloudflare D1 SQLite (Storage & Auto-Pruning)"]
        end
    end

    subgraph Upstream ["xAI Upstream"]
        XAI["xAI Official Web / API (:443)"]
    end

    %% Translation Flow
    Client -->|1. Translation Request JSON| Mod3
    P_INJ --> GATEWAY
    GATEWAY -->|2. Forward Request| XAI
    XAI -->|3. Raw Stream Response| GATEWAY
    GATEWAY -->|4. Return Response| Mod3
    Mod3 -->|5. Validate / Repair / Pass| Client

    %% Registration Flow
    REG -->|Dispatch Challenge| SOLVER
    REG -->|Rotate Domains| CF_DOMAINS
    CF_DOMAINS -->|Catch-all Ingestion| CF_D1
    MAIL_POLL -->|Extract Verification Codes| CF_D1
    REG -->|Egress via Proxy Pool| PROXY_POOL
    PROXY_POOL -->|Sign-up Requests| XAI
    AUTO_IMP -.->|Sync Verified Accounts| POOL
```

---

## Registration Evolution: Pure Protocol to Camoufox Authentic Fingerprint Solving

```
[Phase 1: Pure Protocol]                              [Phase 2: Camoufox Authentic Fingerprint]
Direct HTTP API emulation + 3rd-party solver             Custom-built Firefox C++ core + Local multi-threading
   │                                                        │
   ├── Advantage: Sub-second request speed                  ├── Advantage: Zero 3rd-party fees, local high-concurrency
   └── Fatal Roadblocks:                                    └── Breakthrough:
       1. xAI tightened Cloudflare Turnstile enforcement        1. C++ source-level Canvas/Audio noise injection
       2. Challenge tokens tied to browser fingerprints         2. navigator.webdriver completely eradicated
       3. 3rd-party tokens rejected (UNSOLVABLE / 403)          3. Authentic Bezier human cursor trajectories
       4. Expensive, wasted solver balances                     4. Fast 2-4s local solving, seamless pipeline feed
```

### 1. Demise of Pure Protocol Emulation
Earlier versions emulated registration REST endpoints directly and acquired Turnstile tokens from 3rd-party solver services (e.g. YesCaptcha). However, xAI and Cloudflare upgraded their bot defenses to bind challenge tokens strictly to hardware execution fingerprints (Canvas rendering, WebGL shader traits, AudioContext frequency response, Navigator objects, mouse trajectories, TLS Client Hello JA3/JA4 fingerprints). Detached tokens were rejected with errors like `ERROR_CAPTCHA_UNSOLVABLE` (accounting for **864 production failures / 24.2%**), breaking the pure-protocol strategy entirely.

### 2. Adoption of Camoufox
The suite transitioned to **Camoufox**, an anti-detect browser built from a customized Firefox source:
- **C++ Native Anti-Fingerprinting**: Injects subtle, genuine hardware noise at the engine layer without automated flags like `navigator.webdriver`.
- **Local Multi-Threaded Solver (`turnstile-solver` on port 5072)**: Runs 3+ concurrent worker threads locally, solving Turnstile challenges in 2–4 seconds with a >99% success rate.
- **Zero Ongoing Solver Cost**: Eliminates recurring token purchases while scaling stably to 20,000+ accounts.

---

## Infrastructure Deep Dive

### 1. Proxy & Egress Routing (Mihomo Proxy Pool)
- **Mihomo Kernel Integration**:
  - Connects to a local Mihomo (Clash Meta) instance at `http://127.0.0.1:20172` managing multiple clean residential and data-center egress nodes.
  - Rotates egress IPs dynamically per registration batch or per active session.
- **Egress IP Concurrency & Reuse Limits**:
  - Sending too many rapid registration requests through a single IP triggers Cloudflare WAF challenges or xAI HTTP 429 rate limits.
  - The pipeline checks node health and enforces deliberate request intervals to prevent IP exhaustion.
- **TLS Handshake Resilience (curl 35)**:
  - Transient network blips on overseas VPS proxies can drop OpenSSL handshakes (`Connection reset by peer`, occurring **107 times / 3.0%** in our logs).
  - Built-in exponential backoff with jitter and a 5-second connection timeout ensure resilience against transient failures.

---

### 2. Domain & Email Systems (Cloudflare Email Routing)
- **Serverless Catch-all Email**:
  - Cloudflare Email Routing forwards all catch-all addresses (`*@your-domain.com`) to a Cloudflare Worker, bypassing the need for self-hosted mail servers.
- **Worker Configuration**:
  - Parses inbound emails and writes verification messages into Cloudflare D1 SQLite.
  - Environment variables: `MAIL_DOMAINS` (hosted domain list) and `DOMAIN_COOL_DOWN_MINUTES` (cooling interval).
- **12+ Domain Pool & Hourly Limits**:
  - **Live Domain Pool**: Rotates across 12+ domains (`.space`, `.online`, `.bond`, `.dpdns.org`, `.site`, `.xyz`, `.pp.ua`, `.shop`, `.club`, `.fun`, `.icu`, etc.).
  - **Single-Domain Limit**: Exceeding approximately 15–20 signups per domain within an hour triggers xAI's `email-signup-unavailable` error!
  - **Hard Evidence**: In our analysis of 3,577 historical failures, `email-signup-unavailable` accounted for **2,600 events (72.7%)**—the single biggest rate-limiting hurdle.
  - **Solution**: The scheduler cycles through the 12+ domain pool with a 3,000 ms stagger interval to keep hourly volume well below thresholds.
- **Domain Blacklist Protection**:
  - xAI bans specific heavily-abused free/cheap TLDs (such as `.in`, e.g. `missing.indevs.in`), directly returning `email-domain-rejected` (5 recorded failures). These suffixes are filtered out in advance.

---

### 3. Database & Storage Safety (Cloudflare D1 SQLite)
- **Serverless Storage Architecture**:
  - Cloudflare D1 provides low-latency, distributed SQLite storage with a generous 5 GB free-tier allowance.
- **Why Verification Emails Must Be Cleaned Up Automatically**:
  - Verification codes are short-lived (5–10 minutes). Storing high volumes of raw emails indefinitely risks filling D1 storage and triggering database write errors.
- **Live Health Metrics**:
  - The active `local_mailbox` database occupies only **43.25 MB**, or **0.86%** of the 5 GB quota, operating at an exceptionally healthy utilization level.
- **Dual Automatic Pruning Safeguards**:
  1. **In-flight Worker Auto-Pruning**: Each new inbound email asynchronously triggers SQL to prune verification emails older than 24 hours containing `Grok` or `xAI`.
  2. **Cloudflare Cron Trigger**: Runs every 30 minutes to clean up expired and orphan records, permanently eliminating the risk of database overflows.

---

## Quantified Post-Mortem: 3,577 Production Failures Analyzed

Every failure recorded in production logs across 3,577 incidents was categorized to guide architectural hardening:

| Error Signature | Count | Ratio | Root Cause | Engineering Solution |
| :--- | :---: | :---: | :--- | :--- |
| **`email-signup-unavailable`** | **2,600** | **72.7%** | **Hourly domain rate limit hit** (>15-20 requests/hr per domain). | **12+ domain pool** + **dynamic cooldown** + **3000ms staggered dispatch**. |
| **`YesCaptcha ERROR_CAPTCHA_UNSOLVABLE`** | **864** | **24.2%** | **3rd-party solver rejected** by xAI Turnstile fingerprint upgrades. | **Switched to local Camoufox solver**, achieving >99% pass rate at $0 cost. |
| **`TLS / Proxy Connect Blip (curl 35)`** | **107** | **3.0%** | **Transient proxy connection reset** (`Connection reset by peer`). | **Exponential backoff retry** + **5s connection timeout** + health checks. |
| **`email-domain-rejected`** | **5** | **0.1%** | **Domain TLD blacklisted** by xAI anti-abuse (e.g. `.in`). | **TLD pre-filtering** against known blacklisted extensions. |
| **`Locator Timeout`** | **1** | **<0.1%** | DOM element rendering timeout during rare network lag. | Added element load retries and smart DOM waits. |
| **Total** | **3,577** | **100%** | — | **Post-hardening, the system operates stably across 20,000+ verified accounts.** |

---

## Model Selection Criteria & Degradation Warnings

### Golden Standard: Why Novel Translation is Strictly Locked to grok-4.2

The recommended and locked production model is **`grok-4.2`** (specifically `grok-4.20-reasoning` / `grok-4.20-0309-reasoning`):

1. **Rock-Solid Structural JSON Compliance**:
   - Long-form novel translation splits chapters into numbered line dictionaries (e.g. `{"1": "...", "2": "..."}`). `grok-4.2` strictly respects line numbers, preserves blank lines, and omits conversational filler.
2. **Contextual & Literary Polish**:
   - Accurately captures honorifics, game/litRPG mechanics, idioms, and web novel tropes, producing natural and engaging translations.
3. **High First-Pass Quality**:
   - Across 1,000+ real novel chapters tested with `3-translation-validator`, `grok-4.2` delivered a **96.9% first-pass reasoning release rate**, rarely repeating raw source text.

---

### Pitfalls & Warnings for Other Models (grok-4.6 / grok-3 / grok-2)

We strongly advise **against** using the following models for batch novel translation:

#### ❌ `grok-4.6` Warnings
- **Reasoning Leakage & Truncation**:
  - Forces extensive `<think>...</think>` output blocks that consume tokens and frequently cause output truncation before completing the chapter slice.
- **Frequent `model_busy` 503 Errors**:
  - Under concurrent load, `grok-4.6` frequently throws `model_busy` during stream generation, triggering cascading retries.
- **Strict Account Concurrency Caps**:
  - Very tight hourly quotas quickly trigger 429 errors during heavy batch translation.

#### ❌ `grok-2` / `grok-3` Degradation Warnings
- **Source Repetition (`raw_repetition_exact`)**:
  - Often outputs raw untranslated Korean text when encountering rare words or complex phrasing.
- **Line Dropping & Format Breakdown**:
  - Regularly drops blank line keys (e.g. `{"4": ""}`), causing downstream alignment offset errors.
- **Semantic Drift & Hallucination**:
  - Contextual consistency degrades across long text passages, resulting in erratic tone shifts.

---

## Modules Overview

- **[1-progrok](1-progrok/README.md)**: Turnstile solver (:5072) + Account generation engine (:3080).
- **[2-grok2api](2-grok2api/README.md)**: OpenAI-compatible API reverse proxy (:3001) with safe DB log rotation.
- **[3-translation-validator](3-translation-validator/README.md)**: In-line practical 3-tier validator (:3002) with live telemetry dashboard.

---

## Quickstart (Linux / VPS One-Click Deployment)

```bash
git clone https://github.com/never-seek/grok-translator-suite.git
cd grok-translator-suite
chmod +x deploy.sh manage.sh
./deploy.sh
```

---

## Operations & Management (`./manage.sh`)

```bash
./manage.sh status     # Check health of all services
./manage.sh logs       # Stream combined container logs
./manage.sh restart    # Restart services
./manage.sh cleanup    # Safe SQLite DB cleanup & vacuum
./manage.sh update     # Pull updates & reload
```

- **Translation API Gateway**: `http://YOUR_SERVER_IP:3002/v1`
- **Validator Live Dashboard**: `http://YOUR_SERVER_IP:3002/audit`
- **Grok2API Management Console**: `http://YOUR_SERVER_IP:3001`
- **ProGrok Admin UI**: `http://YOUR_SERVER_IP:3080`

---

## Client Integration Guide (NovelPie / Cherry Studio)

- **API Base URL**: `http://YOUR_SERVER_IP:3002/v1`
- **API Key**: `sk-grok-translator`
- **Primary Model**: `grok-4.20-0309-reasoning`
- **Fallback Model**: `grok-3`

---

## Production Freeze Baseline

```
Window Observation Chunks: 1051
Overall HTTP 200 Success Rate: 98.29%
First-pass Reasoning Release: 96.9%
OPAQUE_LITERAL False Positive: 0
Current D1 Mailbox Utilization: 0.86% (43.25 MB / 5 GB)
Active Account Pool Scale: 20,000+ Accounts
```

---

## License

Released under the [MIT License](LICENSE).
