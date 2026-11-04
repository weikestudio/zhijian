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
T_ITEMS = cfgmod.get("bitable", "items_table_id", default="tbl41NvurOwEquYE")

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


def _find_record_ids_by_field(table_id: str, field_name: str, value: str) -> list[str]:
    """按字段值查 record_id 列表（用于重复写入前清理）"""
    try:
        res = _cli(["+record-list", "--table-id", table_id,
                    "--limit", "200", "--format", "json"])
        d = res.get("data") or {}
        rows = d.get("data") or []
        rid_list = d.get("record_id_list") or []
        fields_meta = d.get("fields") or []
        # fields_meta 可能是字段名列表，找 field_name 对应的列索引
        col_idx = None
        for i, f in enumerate(fields_meta):
            fname = f.get("name") if isinstance(f, dict) else f
            if fname == field_name:
                col_idx = i
                break
        if col_idx is None:
            return []
        ids = []
        for idx, row in enumerate(rows):
            val = row[col_idx] if col_idx < len(row) else None
            if val == value and idx < len(rid_list):
                ids.append(rid_list[idx])
        return ids
    except Exception as e:
        print(f"  查询旧记录失败: {e}", file=sys.stderr)
        return []


def _delete_records(table_id: str, record_ids: list[str]):
    """批量删除记录"""
    if not record_ids:
        return
    for i in range(0, len(record_ids), 50):
        chunk = record_ids[i:i+50]
        try:
            res = _cli(["+record-delete", "--table-id", table_id, "--yes"],
                       {"record_id_list": chunk})
            if not res.get("ok"):
                print(f"  删除旧记录返回失败: {res.get('error',{}).get('message','')[:100]}", file=sys.stderr)
        except Exception as e:
            print(f"  删除旧记录异常: {e}", file=sys.stderr)


def clear_order_records(order_no: str):
    """打包前清理该订单的旧记录（订单关联 + 商品索引），避免重复"""
    for tid, field in [(T_ORDERS, "订单号"), (T_ITEMS, "所属订单号")]:
        rids = _find_record_ids_by_field(tid, field, order_no)
        if rids:
            _delete_records(tid, rids)


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
    """批量写入报告库，按哈希去重：识别成功时覆盖旧记录，待确认时跳过"""
    if not records:
        return
    # 查已有哈希 -> record_id 映射
    hash_to_rid = {}
    try:
        res = _cli(["+record-list", "--table-id", T_REPORTS,
                    "--limit", "200", "--format", "json"])
        d = res.get("data") or {}
        rows = d.get("data") or []
        rid_list = d.get("record_id_list") or []
        fields_meta = d.get("fields") or []
        col_idx = None
        for i, f in enumerate(fields_meta):
            fname = f.get("name") if isinstance(f, dict) else f
            if fname == "哈希":
                col_idx = i
                break
        if col_idx is not None:
            for idx, row in enumerate(rows):
                h = row[col_idx] if col_idx < len(row) else None
                if h and idx < len(rid_list):
                    hash_to_rid[h] = rid_list[idx]
    except Exception:
        pass

    create = []
    to_delete = []
    for rec in records:
        h = rec.get("哈希", "")
        is_review = bool(rec.get("待确认"))
        old_rid = hash_to_rid.get(h)
        if old_rid:
            if is_review:
                # 新的也是待确认，跳过（别覆盖好数据）
                continue
            else:
                # 新的识别成功，删旧写新
                to_delete.append(old_rid)
        create.append(_map_report(rec))

    if to_delete:
        _delete_records(T_REPORTS, to_delete)
        print(f"  更新 {len(to_delete)} 条旧记录（识别更准）")

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


def list_items(page_size: int = 200) -> dict:
    res = _cli(["+record-list", "--table-id", T_ITEMS,
                "--limit", str(page_size), "--format", "json"])
    return res.get("data", {})


# ---------------- 订单商品索引 ----------------

def upsert_items(order_no: str, index_rows: list[dict]):
    """把打包时的商品级索引写入「订单商品索引」表"""
    if not index_rows:
        return 0
    create = []
    for r in index_rows:
        status_raw = (r.get("状态") or "").strip()
        # 归一化状态到选项
        if "过期" in status_raw:
            status = "报告过期"
        elif "无溯源码" in status_raw:
            status = "无溯源码"
        elif "无报告" in status_raw:
            status = "无报告"
        elif "溯源接口失败" in status_raw:
            status = "溯源失败"
        elif status_raw == "正常":
            status = "正常"
        else:
            status = "正常"
        fields = {
            "所属订单号": order_no,
            "序号": r.get("序号", 0),
            "商品": r.get("商品", ""),
            "编码": r.get("编码", ""),
            "数量": float(r.get("数量") or 0),
            "单位": r.get("单位", ""),
            "供应商": r.get("供应商", ""),
            "溯源码": r.get("溯源码", ""),
            "报告文件": r.get("报告文件", ""),
            "检测机构": r.get("检测机构", ""),
            "状态": [status],
            "打包时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if r.get("检测日期"):
            fields["检测日期"] = f"{r['检测日期']} 00:00"
        if r.get("报告有效期"):
            fields["报告有效期"] = f"{r['报告有效期']} 00:00"
        create.append({k: v for k, v in fields.items() if v not in ("", None, 0)})

    written = 0
    for i in range(0, len(create), 100):
        chunk = create[i:i+100]
        res = _cli(["+record-batch-create", "--table-id", T_ITEMS],
                   {"create_records": chunk})
        if res.get("ok"):
            written += len(chunk)
        else:
            print(f"  商品索引写入失败: {res.get('error')}", file=sys.stderr)
    return written


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
