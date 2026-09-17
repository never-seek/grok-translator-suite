#!/usr/bin/env bash
# ==============================================================================
# Grok Translator Suite - 日常运维管理脚本
# 用法: ./manage.sh [status|start|stop|restart|logs|cleanup|update]
# ==============================================================================

set -euo pipefail

# 自动选择 docker compose 命令
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "[错误] 未检测到 docker compose！"
    exit 1
fi

ACTION="${1:-status}"

case "$ACTION" in
    status)
        echo "=== 正在获取容器运行状态 ==="
        $COMPOSE ps
        ;;
    start)
        echo "=== 正在启动所有服务 ==="
        $COMPOSE up -d
        echo "=== 启动指令已发送 ==="
        ;;
    stop)
        echo "=== 正在停止所有服务 ==="
        $COMPOSE down
        echo "=== 所有服务已停止 ==="
        ;;
    restart)
        echo "=== 正在重启所有服务 ==="
        $COMPOSE restart
        echo "=== 所有服务已重启 ==="
        ;;
    logs)
        SERVICE="${2:-}"
        if [ -n "$SERVICE" ]; then
            $COMPOSE logs -f "$SERVICE"
        else
            $COMPOSE logs -f --tail=100
        fi
        ;;
    cleanup)
        echo "=== 正在执行数据库与历史审计日志安全清理 ==="
        DB_PATH="./2-grok2api/data/backend.db"
        if [ -f "$DB_PATH" ]; then
            bash ./2-grok2api/scripts/cleanup-db-safe.sh "$DB_PATH"
        else
            echo "[提示] 数据库文件暂不存在: $DB_PATH"
        fi
        ;;
    update)
        echo "=== 正在拉取最新镜像并重新构建服务 ==="
        $COMPOSE pull grok2api
        $COMPOSE up -d --build
        echo "=== 服务更新完成 ==="
        ;;
    *)
        echo "用法: $0 {status|start|stop|restart|logs [服务名]|cleanup|update}"
        exit 1
        ;;
esac
