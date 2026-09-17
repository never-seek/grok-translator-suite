#!/usr/bin/env bash
# ==============================================================================
# Grok Translator Suite - Linux / VPS 一键自动化部署脚本
# 适用系统: Ubuntu 20.04+, Debian 11+, CentOS 8+, RockyLinux, AlmaLinux
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
cat << "EOF"
  ____            _      _____                      _       _             
 / ___|_ __ ___  | | __ |_   _| __ __ _ _ __  ___  | | __ _| |_ ___  _ __ 
| |  _| '__/ _ \ | |/ /   | || '__/ _` | '_ \/ __| | |/ _` | __/ _ \| '__|
| |_| | | | (_) ||   <    | || | | (_| | | | \__ \ | | (_| | || (_) | |   
 \____|_|  \___/ |_|\_\   |_||_|  \__,_|_| |_|___/ |_|\__,_|\__\___/|_|   
                     S  U  I  T  E  -  v1.0.0
EOF
echo -e "${NC}"
echo -e "${BLUE}=== 专为长篇网络小说批量机翻打造的生产级 Grok 基础设施套件 ===${NC}\n"

# 1. 检测运行环境与依赖工具
echo -e "${YELLOW}[1/5] 检查系统环境与基础依赖...${NC}"

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

if ! command_exists docker; then
    echo -e "${YELLOW}未检测到 Docker，正在尝试自动安装官方 Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker || service docker start || true
fi

# 检查 Docker Compose (v2 插件或独立版)
if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command_exists docker-compose; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[错误] 未检测到 docker compose 插件或 docker-compose 命令！${NC}"
    echo "请执行: apt-get install docker-compose-plugin 或 yum install docker-compose-plugin"
    exit 1
fi
echo -e "${GREEN}✓ Docker 及 Docker Compose 环境就绪 (${DOCKER_COMPOSE_CMD})${NC}"

# 2. 自动生成高强度安全随机密钥（彻底脱敏，千人千密）
echo -e "${YELLOW}[2/5] 初始化安全配置文件与独立随机密钥...${NC}"

generate_hex_secret() {
    if command_exists openssl; then
        openssl rand -hex 32
    else
        python3 -c "import secrets; print(secrets.token_hex(32))"
    fi
}

generate_b64_secret() {
    if command_exists openssl; then
        openssl rand -base64 32
    else
        python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
    fi
}

generate_password() {
    if command_exists openssl; then
        openssl rand -hex 8
    else
        python3 -c "import secrets; print(secrets.token_urlsafe(12))"
    fi
}

JWT_SECRET=$(generate_hex_secret)
ENC_KEY=$(generate_b64_secret)
ADMIN_PASS=$(generate_password)

# 创建目录结构
mkdir -p ./2-grok2api/data
mkdir -p ./3-translation-validator/config ./3-translation-validator/logs
mkdir -p ./1-progrok/config

# 3. 配置 .env
if [ ! -f ".env" ]; then
    echo "正在从 .env.example 生成独立 .env..."
    cp .env.example .env
    # 替换其中的默认密码
    sed -i "s/CHANGE_THIS_ADMIN_PASSWORD/$ADMIN_PASS/g" .env || true
fi

# 4. 配置 2-grok2api/config.yaml
if [ ! -f "./2-grok2api/config.yaml" ]; then
    echo "正在初始化 Grok2API 专属密钥与配置文件..."
    cp ./2-grok2api/config.example.yaml ./2-grok2api/config.yaml
    sed -i "s/YOUR_64_CHAR_HEX_JWT_SECRET_KEY_REPLACE_ME_NOW/$JWT_SECRET/g" ./2-grok2api/config.yaml || true
    sed -i "s|YOUR_32_BYTE_BASE64_CREDENTIAL_ENCRYPTION_KEY=|$ENC_KEY|g" ./2-grok2api/config.yaml || true
    sed -i "s/CHANGE_THIS_SECURE_ADMIN_PASSWORD/$ADMIN_PASS/g" ./2-grok2api/config.yaml || true
fi

# 5. 配置 1-progrok/config/config.json
if [ ! -f "./1-progrok/config/config.json" ]; then
    echo "正在初始化 ProGrok 联动配置..."
    cp ./1-progrok/config.example.json ./1-progrok/config/config.json
    sed -i "s/CHANGE_THIS_ADMIN_PASSWORD/$ADMIN_PASS/g" ./1-progrok/config/config.json || true
fi

# 6. 配置 3-translation-validator/config/translation_profiles.json
if [ ! -f "./3-translation-validator/config/translation_profiles.json" ]; then
    cp ./3-translation-validator/config.example.json ./3-translation-validator/config/translation_profiles.json
fi

echo -e "${GREEN}✓ 密钥与配置自动初始化完成。${NC}"

# 3. 预初始化纯净数据库
if [ ! -f "./2-grok2api/data/backend.db" ] && command_exists sqlite3; then
    echo -e "${YELLOW}[3/5] 正在预载入纯净 SQLite 数据库表结构...${NC}"
    sqlite3 ./2-grok2api/data/backend.db < ./2-grok2api/init_db.sql || true
    echo -e "${GREEN}✓ 数据库架构初始化完毕。${NC}"
else
    echo -e "${YELLOW}[3/5] 数据库架构将由网关容器首次启动自动映射。${NC}"
fi

# 4. 启动容器集群
echo -e "${YELLOW}[4/5] 正在拉起 Docker 容器集群 (首次启动需构建轻量中间件)...${NC}"
$DOCKER_COMPOSE_CMD up -d --build

# 5. 显示部署结果与连接信息
echo -e "\n${YELLOW}[5/5] 获取公网访问入口与状态信息...${NC}"

SERVER_IP=$(curl -s4 --connect-timeout 3 ifconfig.me 2>/dev/null || curl -s4 --connect-timeout 3 icanhazip.com 2>/dev/null || echo "你的服务器IP")

echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN}🎉 Grok Translator Suite 全栈服务已成功部署并启动！${NC}"
echo -e "${GREEN}================================================================${NC}\n"

echo -e "🔹 ${CYAN}1. 翻译软件对接入口 (NovelPie / Cherry Studio 等)${NC}"
echo -e "   • 接口地址 (Base URL) : ${YELLOW}http://${SERVER_IP}:3002/v1${NC}"
echo -e "   • API Key             : ${YELLOW}sk-grok-translator${NC} (或任意非空字符串)"
echo -e "   • 主选推理模型        : ${GREEN}grok-4.20-0309-reasoning${NC}"
echo -e "   • 兜底重试模型        : ${GREEN}grok-3${NC}"

echo -e "\n🔹 ${CYAN}2. 实用三级质检监控看板 (Practical Validator Dashboard)${NC}"
echo -e "   • 访问地址            : ${YELLOW}http://${SERVER_IP}:3002/audit${NC}"
echo -e "   • 包含指标            : 请求耗时、三级判定通过率、韩文复读拦截、主备降级详情"

echo -e "\n🔹 ${CYAN}3. Grok2API 反代与账号池管理网关${NC}"
echo -e "   • 管理后台            : ${YELLOW}http://${SERVER_IP}:3001${NC}"
echo -e "   • 默认账号            : ${YELLOW}admin${NC}"
echo -e "   • 初始密码            : ${YELLOW}${ADMIN_PASS}${NC} (已保存在 2-grok2api/config.yaml)"

echo -e "\n🔹 ${CYAN}4. ProGrok 全协议极速注册控制台${NC}"
echo -e "   • Web 控制台          : ${YELLOW}http://${SERVER_IP}:3080${NC}"
echo -e "   • 功能                : 批量协议产号、邮箱验证码自动提取、一键入库 Grok2API"

echo -e "\n${BLUE}💡 常用管理命令:${NC}"
echo -e "   • 查看运行状态 : ${CYAN}./manage.sh status${NC}"
echo -e "   • 查看实时日志 : ${CYAN}./manage.sh logs${NC}"
echo -e "   • 重启全部服务 : ${CYAN}./manage.sh restart${NC}"
echo -e "   • 安全清理日志 : ${CYAN}./manage.sh cleanup${NC}"
echo -e "${GREEN}================================================================${NC}\n"
