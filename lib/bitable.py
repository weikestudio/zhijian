#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书多维表格读写：报告库 + 订单关联"""
from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config as cfgmod

BASE = cfgmod.get("bitable", "app_token", default="SYnubkNJDa8sYus9TdKcUmFfnYb")
T_REPORTS = cfgmod.get("bitable", "reports_table_id", default="tblzyCNRP2unTTUF")
T_ORDERS = cfgmod.get("bitable", "orders_table_id", default="tblsY22CdrlTN0Qe")

LEVEL_PREFIX = {
    "L0": "L0_资质证件", "L1": "L1_法定检验", "L2": "L2_出厂检验",
    "L3": "L3_快检筛查", "L?": "L?_未识别",
}


def _cli(args: list, payload: dict | None = None) -> dict:
    """调 lark-cli base，返回解析后的 JSON"""
    cmd = ["lark-cli", "base"] + args + ["--base-token", BASE, "--as", "user", "--format", "json"]
    if payload is not None:
        cmd += ["--json", json.dumps(payload, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f"lark-cli 返回非 JSON: {r.stdout[:300]} {r.stderr[:300]}")


# ---------------- 报告库 ----------------

def _map_report(rec: dict) -> dict:
    """把 qc_ingest 输出的一行映射成多维表格字段"""
    level = rec.get("报告等级", "L?")
    fields = {
        "文件名": rec.get("源文件", ""),
        "报告等级": [LEVEL_PREFIX.get(level, "L?_未识别")],
        "报告类型": rec.get("报告类型", ""),
        "检测项目": rec.get("检测项目", ""),
        "检测机构": rec.get("机构", ""),
        "产品SKU": rec.get("产品", ""),
        "溯源码批次": rec.get("溯源码批次", ""),
        "有CMA": rec.get("有CMA标志") == "是",
        "有章": rec.get("有章迹描述") == "是",
        "本地路径": rec.get("归档路径", ""),
        "蔬东坡URL": rec.get("蔬东坡URL", ""),
        "大小KB": rec.get("大小KB", 0),
        "哈希": rec.get("哈希", ""),
        "置信度": rec.get("置信度", 0),
        "待确认": rec.get("待确认", ""),
        "入库时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    d = rec.get("日期") or ""
    if d:
        fields["报告日期"] = f"{d} 00:00"
    # 去掉空字符串，避免飞书报错
    return {k: v for k, v in fields.items() if v not in ("", None)}


def upsert_reports(records: list[dict]):
    """批量写入报告库（简单模式：直接新增，按哈希去重由调用方保证）"""
    if not records:
        return
    create = []
    for rec in records:
        create.append(_map_report(rec))
    # 飞书批量创建上限 200
    for i in range(0, len(create), 100):
        chunk = create[i:i+100]
        res = _cli(["+record-batch-create", "--table-id", T_REPORTS],
                   {"create_records": chunk})
        if not res.get("ok"):
            print(f"  bitable 写入失败: {res.get('error')}", file=sys.stderr)


def list_reports(page_size: int = 50, filter_text: str = "") -> dict:
    """读报告库列表，给 Web 工作台用"""
    args = ["+record-list", "--table-id", T_REPORTS,
            "--limit", str(page_size), "--format", "json"]
    if filter_text:
        # 简单模糊：在 jq 里过滤
        pass
    res = _cli(args)
    return res.get("data", {})


# ---------------- 订单关联 ----------------

def upsert_order(order_info: dict):
    """写入一条订单关联记录
    order_info: {order_no, delivery_date, customer, items, reports, issues, missing, pack_pdf}
    """
    fields = {
        "订单号": order_info.get("order_no", ""),
        "客户": order_info.get("customer", ""),
        "商品行数": order_info.get("items", 0),
        "报告数": order_info.get("reports", 0),
        "异常数": order_info.get("issues", 0),
        "缺报告商品": "; ".join(order_info.get("missing", [])[:20]),
        "合订本路径": order_info.get("pack_pdf", ""),
        "打包时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    d = order_info.get("delivery_date") or ""
    if d:
        fields["订单日期"] = f"{d} 00:00"
    fields = {k: v for k, v in fields.items() if v not in ("", None)}
    res = _cli(["+record-batch-create", "--table-id", T_ORDERS],
               {"create_records": [fields]})
    return res.get("ok", False)


def list_orders(page_size: int = 50) -> dict:
    res = _cli(["+record-list", "--table-id", T_ORDERS,
                "--limit", str(page_size), "--format", "json"])
    return res.get("data", {})


# ---------------- 统计 ----------------

def stats() -> dict:
    """仪表盘统计"""
    rep = _cli(["+record-list", "--table-id", T_REPORTS, "--limit", "1", "--format", "json"])
    ord_ = _cli(["+record-list", "--table-id", T_ORDERS, "--limit", "1", "--format", "json"])
    def total(d):
        return (d.get("total") or len(d.get("data") or []) or 0)
    return {
        "reports_total": total(rep.get("data", {})),
        "orders_total": total(ord_.get("data", {})),
    }


if __name__ == "__main__":
    # 自检
    print("base:", BASE)
    print("reports table:", T_REPORTS)
    print("orders table:", T_ORDERS)
    print("stats:", stats())
