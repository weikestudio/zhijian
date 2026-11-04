#!/bin/bash
# 一键安装 qc-report-keeper
# 用法: curl -sSL <repo>/install.sh | bash  或  ./install.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "==> qc-report-keeper 安装"

# 1. 检测 Python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 python3，请先安装 Python 3.10+"
  exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYVER  ($(which python3))"

# 2. 装依赖（优先用 workbuddy 解释器，没有就用系统 python3 -m pip --user）
PIP=""
if [ -x "/Users/v1/.workbuddy/binaries/python/envs/default/bin/python" ]; then
  PIP="/Users/v1/.workbuddy/binaries/python/envs/default/bin/python -m pip"
else
  PIP="python3 -m pip --user"
fi
echo "==> 安装 Python 依赖..."
$PIP install -q -r requirements.txt

# 3. 编译 OCR（macOS Vision）
if [ ! -x tools/ocr ] || [ tools/ocr.swift -nt tools/ocr ]; then
  echo "==> 编译 OCR (tools/ocr)..."
  (cd tools && swiftc -O ocr.swift -o ocr)
fi
echo "✓ tools/ocr"

# 4. 配置文件
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "✓ 已生成 config.yaml（请填入真实 appid/secret）"
else
  echo "✓ config.yaml 已存在，跳过"
fi

# 5. 建归档根目录
python3 -c "import yaml; c=yaml.safe_load(open('config.yaml')); import os; os.makedirs(c['storage']['library_root'], exist_ok=True); os.makedirs(c['storage']['pack_out_dir'], exist_ok=True)"

# 6. 豆包 Skill 软链
USER_SKILLS="/Users/v1/Library/Application Support/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.user_skills"
if [ -d "$USER_SKILLS" ]; then
  ln -sfn "$REPO_DIR" "$USER_SKILLS/qc-report-keeper"
  echo "✓ 已在 .user_skills/ 下软链接 qc-report-keeper"
fi

echo ""
echo "✅ 安装完成"
echo "   启动工作台:  ./bin/qc serve"
echo "   环境自检:    ./bin/qc doctor"
echo "   配置文件:    $REPO_DIR/config.yaml"
