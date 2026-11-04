# qc-report-keeper

生鲜质检报告管家：识别、归档、索引、按订单打包。

## 功能

- **报告入库**：供应商发来的图片/PDF 自动 OCR、按法律效力分级（L0-L3）、本地归档、写入飞书多维表格
- **按订单打包**：根据蔬东坡订单号自动拉取商品溯源报告，合并成无抬头合订本 PDF 用于随车打印
- **可视化工作台**：本地 Web 界面查看报告库、触发打包、仪表盘统计
- **定时任务**：每日自动跑当日订单打包

## 安装

```bash
git clone <你的仓库地址> qc-report-keeper
cd qc-report-keeper
./install.sh
```

install.sh 会：
1. 检测 Python3
2. 安装依赖（pypdf、flask、pyyaml 等）
3. 编译 macOS Vision OCR（tools/ocr）
4. 生成 config.yaml（从 config.example.yaml 复制）
5. 在豆包 .user_skills 下建软链接

## 配置

编辑 `config.yaml`：

```yaml
sdp:
  appid: "你的appid"
  secret: "你的secret"
  project_code: "cc_xdsl"

storage:
  library_root: "/path/to/library"

bitable:
  app_token: "飞书多维表格token"
  reports_table_id: "tbl..."
  orders_table_id: "tbl..."
```

## 使用

```bash
# 启动工作台
./bin/qc serve

# 环境自检
./bin/qc doctor

# 报告入库
./bin/qc ingest --inbox ~/Downloads/供应商报告

# 按订单打包
./bin/qc pack --order DD261103120159001952
./bin/qc pack --date 2026-11-02
```

## 目录结构

```
qc-report-keeper/
├── SKILL.md           # 豆包 Skill 入口
├── install.sh         # 一键安装
├── config.example.yaml
├── requirements.txt
├── bin/qc             # 命令行
├── lib/               # Python 模块
├── tools/ocr.swift    # OCR 源码
└── web/index.html     # 工作台前端
```
