#!/bin/bash
# 第一次用之前，双击这个跑一次：建一个独立 Python 环境并装好依赖。
# 之后就不用再跑了，直接双击「微信导出.app」。
set -e
GUIDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$GUIDIR/.."   # 到仓库根目录（requirements.txt 在这）

echo "———————————————————————————————"
echo " 微信导出向导 · 安装环境"
echo "———————————————————————————————"

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "✗ 没找到 python3。请先装 Python：https://www.python.org/downloads/"
  echo "  装完再双击这个文件一次。"
  read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi
echo "● 用的 Python：$($PY --version)"

echo "● 建独立环境 .venv（不污染系统）…"
"$PY" -m venv .venv

echo "● 装依赖（第一次会下载，稍等）…"
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "● 生成「微信导出.app」…"
cd "$GUIDIR"
rm -rf "微信导出.app"
osacompile -o "微信导出.app" launcher.applescript

echo ""
echo "✓ 装好了！现在双击同一文件夹里的「微信导出.app」就能用。"
read -n 1 -s -r -p "按任意键关闭…"
