---
name: qc-report-keeper
description: 生鲜质检报告管家。用于：① 识别供应商发来的质检报告图片/PDF（快检单、出厂检验、法定检验、资质证件），自动按法律效力分级（L0/L1/L2/L3）、本地归档、写入飞书多维表格；② 根据蔬东坡 SAAS 订单号自动拉取商品溯源报告，合并成无抬头的合订本 PDF 用于随车打印给学校食堂/客户；③ 启动本地可视化工作台查看报告库、触发打包。当用户提到质检报告、检测报告、快检单、报告入库、按订单打包、合订本PDF、蔬东坡报告、供应商报告归档时使用。
---

# 生鲜质检报告管家

服务登封中通云配的生鲜配送质检流程。对接蔬东坡 SAAS（cc_xdsl 环境）+ 飞书多维表格索引。

## 目录

```
qc-report-keeper/
├── config.yaml          # 本地配置（appid/secret/路径，已 gitignore）
├── bin/qc               # 命令行入口
├── lib/
│   ├── config.py        # 配置加载
│   ├── sdp_client.py    # 蔬东坡 API 客户端（已连通）
│   ├── qc_ingest.py     # 报告识别 + 归档 + 分级
│   ├── qc_pack.py       # 按订单打包合订本 PDF（无抬头，纯拼原件）
│   ├── bitable.py       # 飞书多维表格读写
│   └── server.py        # 本地 Web 工作台（Flask，端口 8765）
├── tools/ocr.swift      # macOS Vision OCR 源码
└── web/index.html       # 工作台前端
```

## 三个核心命令

Python 解释器固定用 `/Users/v1/.workbuddy/binaries/python/envs/default/bin/python`（依赖已装）。

### 1. 被动入库：用户拖图片/PDF 进来

用户发图片或 PDF 要求识别/归档时：

```bash
# 把用户发的文件放到临时目录，然后跑入库
mkdir -p /tmp/qc_inbox && cp <用户文件> /tmp/qc_inbox/
PYTHONPATH=lib <PY> lib/qc_ingest.py --inbox /tmp/qc_inbox
```

入库逻辑：
- OCR 提取文本 → 自动判级 L0(资质)/L1(法定CMA)/L2(出厂)/L3(快检)
- 归档到 `~/Vonework/library/Lx_类型/年月/机构_产品_日期.pdf`
- 低置信度进 `_review/` 待人工确认
- 自动写入飞书多维表格「报告库」表

### 2. 按订单打包合订本

用户说"给订单 DDxxx 打包"或"打包今天的报告"：

```bash
# 单订单
PYTHONPATH=lib <PY> lib/qc_pack.py --order DD261103120159001952

# 一天的全部订单
PYTHONPATH=lib <PY> lib/qc_pack.py --date 2026-11-02
```

输出在 `~/Vonework/library/_packed/<订单号>/订单质检报告合订本.pdf`。
**不加封面抬头、不做目录**，原汁原味拼报告原件；只有缺报告时才加一页极简提示。

打包完成后调用 `bitable.upsert_order()` 写入「订单关联」表。

### 3. 启动可视化工作台

用户说"打开工作台"或"看报告库"：

```bash
PYTHONPATH=lib <PY> lib/server.py --port 8765
# 然后用浏览器打开 http://127.0.0.1:8765
```

## 关键约束（来自真实数据实测，别踩）

1. **蔬东坡只能读不能写**：没有上传报告/绑定溯源的接口。
2. **一份快检单覆盖多个 SKU**：多对多关系，按报告文件归组反查，不要按商品一对一校。
3. **报告过期判定**：以订单发货日期 vs 报告 expire_date 为准。
4. **快检单结果"阴性"是定性**：trace_value=0.0000 不是数据缺失，规则引擎别误报。
5. **PDF 可能 AES 加密**：pypdf 需要 cryptography 库。
6. **URL 带中文文件名**：必须 urllib.parse.quote 后再下载。
7. **一个溯源码只返回最新批次**：历史批次靠本地台账+多维表格自己沉淀。

## 报告分级（法律效力）

| 等级 | 类型 | 能证明什么 |
|---|---|---|
| L1 | 法定检验（CMA/CNAS） | 可对外举证 |
| L2 | 出厂检验（厂家自检） | 厂家自检过 |
| L3 | 快检筛查（酶抑制率） | 企业内部质控，不能对外 |
| L0 | 资质证件 | 主体合规 |

## 安全

- appid/secret 在 `config.yaml`，已 gitignore，不入库。
- 如 secret 泄露，去蔬东坡后台重置后改 config.yaml。
