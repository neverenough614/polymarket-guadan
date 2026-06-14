#!/usr/bin/env bash
# predict.fun VPS 一键环境配置（幂等，可重复跑）：swap + 系统依赖 + venv + 安装。
# 前置：仓库已 clone 到 $REPO_DIR（默认 /root/poly-maker）。不碰 .env / 不起服务 / 不下单。
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/poly-maker}"

echo "==> [1/4] swap（2G，防发现阶段 OOM）"
if swapon --show | grep -q .; then
  echo "    已有 swap，跳过"
else
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    已挂 2G swap"
fi

echo "==> [2/4] 系统依赖（python venv/pip/git）"
apt-get update -y
apt-get install -y python3-venv python3-pip git

echo "==> [3/4] 创建 venv"
cd "$REPO_DIR"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --upgrade pip

echo "==> [4/4] 安装依赖（含 predict-sdk extra）"
./.venv/bin/pip install -e ".[predictfun]"

echo ""
echo "✅ 环境就绪。下一步："
echo "   1) cp deploy/.env.example .env  然后填私钥等（见 DEPLOY.md）"
echo "   2) scp 上传 credentials.json 到 $REPO_DIR/"
echo "   3) 跑 dryrun 自检：./.venv/bin/python scripts/predictfun_update_markets.py dryrun"
echo "   4) 装服务：见 DEPLOY.md '安装 systemd 服务' 一节"
