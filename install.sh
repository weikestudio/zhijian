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

# 2. 装依赖
echo "==> 安装 Python 依赖..."
python3 -m pip install --user -q -r requirements.txt

# 3. 编译 OCR（仅 macOS 有 swiftc 时）
if [[ "$OSTYPE" == "darwin"* ]] && command -v swiftc >/dev/null 2>&1; then
  if [ ! -x tools/ocr ] || [ tools/ocr.swift -nt tools/ocr ]; then
    echo "==> 编译 OCR (tools/ocr)..."
    (cd tools && swiftc -O ocr.swift -o ocr)
  fi
  echo "✓ tools/ocr (macOS Vision)"
else
  echo "  ⚠ 非 macOS 或无 swiftc，跳过 OCR 编译，将使用 RapidOCR"
fi

# 4. 配置文件（自动生成 + 交互式填入）
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo ""
  echo "==> 配置蔬东坡 API（按回车跳过，稍后手动改 config.yaml）"
  read -p "  蔬东坡 appid: " appid
  read -p "  蔬东坡 secret: " secret
  read -p "  蔬东坡 project_code [cc_xdsl]: " pcode
  pcode=${pcode:-cc_xdsl}
  if [ -n "$appid" ]; then
    python3 -c "
import yaml
c = yaml.safe_load(open('config.yaml'))
c['sdp']['appid'] = '$appid'
c['sdp']['secret'] = '$secret'
c['sdp']['project_code'] = '$pcode'
yaml.dump(c, open('config.yaml', 'w'), allow_unicode=True)
print('  ✓ config.yaml 已写入')
"
  else
    echo "  ⚠ 已生成 config.yaml 模板，请手动填入 appid/secret"
  fi
else
  echo "✓ config.yaml 已存在，跳过"
fi

# 5. 建归档根目录
python3 -c "
import yaml, os
c = yaml.safe_load(open('config.yaml'))
for k in ['library_root', 'inbox_dir', 'pack_out_dir']:
    p = os.path.expanduser(c['storage'][k])
    os.makedirs(p, exist_ok=True)
    print(f'  ✓ {k}: {p}')
"

# 6. 豆包 Skill 软链（如果存在豆包目录）
USER_SKILLS="$HOME/Library/Application Support/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.user_skills"
if [ -d "$USER_SKILLS" ]; then
  ln -sfn "$REPO_DIR" "$USER_SKILLS/qc-report-keeper"
  echo "✓ 已在 .user_skills/ 下软链接 qc-report-keeper"
fi

echo ""
echo "✅ 安装完成"
echo "   启动工作台:  python3 bin/qc serve"
echo "   环境自检:    python3 bin/qc doctor"
echo "   配置文件:    $REPO_DIR/config.yaml"
