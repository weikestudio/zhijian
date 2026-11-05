#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只同步飞书多维表格，不重新打包 PDF。
从 _packed/ 下已有的 00_索引.csv 读取数据，直接写入飞书。
用法: python sync_bitable.py [--date 2026-11-04]
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import config as cfgmod
import bitable


def sync_one_order(order_dir: Path) -> dict:
    """同步单个订单到飞书"""
    order_no = order_dir.name
    # 三级目录: 客户名/日期/订单号
    delivery_date = order_dir.parent.name
    customer = order_dir.parent.parent.name

    idx_csv = order_dir / "00_索引.csv"
    if not idx_csv.exists():
        return {"order_no": order_no, "ok": False, "error": "无索引文件"}

    # 读索引 CSV
    index_rows = []
    missing = []
    report_files = set()
    with open(idx_csv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            status = row.get("状态", "")
            report = row.get("报告文件", "")
            product = row.get("商品", "")
            index_rows.append({
                "商品名称": product,
                "数量": row.get("数量", ""),
                "单位": row.get("单位", ""),
                "供应商": row.get("供应商", ""),
                "溯源码": row.get("溯源码", ""),
                "报告文件": report,
                "检测机构": row.get("检测机构", ""),
                "检测日期": row.get("检测日期", ""),
                "报告有效期": row.get("报告有效期", ""),
                "状态": status,
            })
            if report:
                report_files.add(report)
            if "无报告" in status:
                missing.append(f"{product}（无报告文件）")
            elif "过期" in status:
                expire = row.get("报告有效期", "")
                missing.append(f"{product}（报告过期 {expire}）")

    # 清旧写新
    try:
        bitable.clear_order_records(order_no)
        bitable.upsert_items(order_no, index_rows)
        bitable.upsert_order({
            "order_no": order_no,
            "delivery_date": delivery_date,
            "customer": customer,
            "items": len(index_rows),
            "reports": len(report_files),
            "issues": len(missing),
            "missing": missing,
            "pack_pdf": str(order_dir / "订单质检报告合订本.pdf"),
        })
        return {"order_no": order_no, "ok": True,
                "items": len(index_rows), "issues": len(missing)}
    except Exception as e:
        return {"order_no": order_no, "ok": False, "error": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="配送日期 YYYY-MM-DD")
    args = ap.parse_args()

    outroot = cfgmod.pack_out_dir()
    date_dir = args.date

    # 找所有该日期的订单目录
    order_dirs = sorted(outroot.glob(f"*/{date_dir}/DD*"))
    print(f"找到 {len(order_dirs)} 个订单目录，开始同步飞书...")

    ok_count = 0
    fail_count = 0
    for i, d in enumerate(order_dirs, 1):
        r = sync_one_order(d)
        if r["ok"]:
            ok_count += 1
            print(f"  [{i}/{len(order_dirs)}] ✓ {r['order_no']}  商品{r['items']}  异常{r['issues']}")
        else:
            fail_count += 1
            print(f"  [{i}/{len(order_dirs)}] ✗ {r['order_no']}  {r['error']}")

    print(f"\n{'='*60}")
    print(f"同步完成: 成功 {ok_count} 单, 失败 {fail_count} 单")


if __name__ == "__main__":
    main()
