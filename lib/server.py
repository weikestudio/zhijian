#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地可视化工作台: qc serve"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import config as cfgmod
import bitable

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
JOBS_FILE = cfgmod.library_root() / "_packed" / "_jobs.json"

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")


# ---------- 任务状态（落盘） ----------
def _load_jobs():
    if JOBS_FILE.exists():
        try:
            return json.loads(JOBS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_jobs(jobs):
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    except Exception:
        pass


PACK_JOBS = _load_jobs()


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# ---------- 仪表盘 ----------
@app.route("/api/stats")
def api_stats():
    s = bitable.stats()
    lib = cfgmod.library_root()
    file_count = sum(1 for _ in lib.rglob("*") if _.is_file()) if lib.exists() else 0

    # 缺报告/过期计数（从商品索引表扫）
    missing_count = 0
    expired_count = 0
    try:
        items = bitable.list_items(page_size=200)
        for row in items.get("data") or []:
            # 状态字段在数组里
            for cell in (row if isinstance(row, list) else [row]):
                if isinstance(cell, list):
                    for v in cell:
                        if v == "无报告":
                            missing_count += 1
                        elif v == "报告过期":
                            expired_count += 1
    except Exception:
        pass

    review_dir = lib / "_review"
    review_count = len(list(review_dir.glob("*"))) if review_dir.exists() else 0

    return jsonify({
        "reports_total": s.get("reports_total", 0),
        "orders_total": s.get("orders_total", 0),
        "local_files": file_count,
        "missing_count": missing_count,
        "expired_count": expired_count,
        "review_count": review_count,
        "library_root": str(lib),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ---------- 报告库 ----------
@app.route("/api/reports")
def api_reports():
    try:
        data = bitable.list_reports(page_size=int(request.args.get("limit", 50)))
        return jsonify(data)
    except Exception as e:
        return jsonify({"fields": [], "data": [], "error": str(e)[:200]}), 200


# ---------- 订单关联 ----------
@app.route("/api/orders")
def api_orders():
    try:
        data = bitable.list_orders(page_size=int(request.args.get("limit", 200)))
        return jsonify(data)
    except Exception as e:
        return jsonify({"fields": [], "data": [], "error": str(e)[:200]}), 200


# ---------- 打包 ----------
@app.route("/api/pack", methods=["POST"])
def api_pack():
    body = request.get_json(force=True)
    order_no = (body.get("order_no") or "").strip()
    date = (body.get("date") or "").strip()
    if not order_no and not date:
        return jsonify({"ok": False, "error": "需要 order_no 或 date"}), 400

    job_id = f"pack_{int(time.time())}"
    cmd = [sys.executable, str(ROOT / "bin" / "qc"), "pack"]
    if order_no:
        cmd += ["--order", order_no]
    if date:
        cmd += ["--date", date]

    def run():
        PACK_JOBS[job_id] = {"status": "running", "start": datetime.now().isoformat(),
                             "log": "", "target": order_no or date}
        _save_jobs(PACK_JOBS)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
            PACK_JOBS[job_id].update({
                "status": "done" if r.returncode == 0 else "failed",
                "log": (r.stdout + "\n" + r.stderr)[-4000:],
                "end": datetime.now().isoformat(),
            })
        except Exception as e:
            PACK_JOBS[job_id].update({"status": "failed", "log": str(e)})
        _save_jobs(PACK_JOBS)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/pack/<job_id>")
def api_pack_status(job_id):
    return jsonify(PACK_JOBS.get(job_id, {"status": "unknown"}))


@app.route("/api/jobs")
def api_jobs():
    """历史打包任务列表"""
    jobs = sorted(PACK_JOBS.items(), key=lambda x: x[1].get("start", ""), reverse=True)
    return jsonify([{"job_id": k, **v} for k, v in jobs[:20]])


# ---------- 入库 ----------
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    body = request.get_json(silent=True) or {}
    inbox = body.get("inbox")
    cmd = [sys.executable, str(ROOT / "bin" / "qc"), "ingest"]
    if inbox:
        cmd += ["--inbox", inbox]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    return jsonify({
        "ok": r.returncode == 0,
        "log": (r.stdout + "\n" + r.stderr)[-4000:],
    })


# ---------- 待确认 ----------
@app.route("/api/review")
def api_review():
    import csv
    lib = cfgmod.library_root()
    review_dir = lib / "_review"
    files = []
    if review_dir.exists():
        for p in sorted(review_dir.glob("*")):
            if p.is_file():
                files.append({
                    "name": p.name,
                    "size_kb": round(p.stat().st_size / 1024),
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    idx_csv = lib / "_index.csv"
    reasons = {}
    if idx_csv.exists():
        with open(idx_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("待确认"):
                    reasons[row.get("源文件", "")] = {
                        "reason": row["待确认"],
                        "level": row.get("报告等级", ""),
                        "product": row.get("产品", ""),
                    }
    for f in files:
        info = reasons.get(f["name"], {})
        f["reason"] = info.get("reason", "")
        f["guess_level"] = info.get("level", "")
        f["product"] = info.get("product", "")
    return jsonify({"files": files, "count": len(files)})


# ---------- 下载/预览合订本 ----------
@app.route("/api/download/<path:relpath>")
def api_download(relpath):
    root = cfgmod.pack_out_dir()
    fp = (root / relpath).resolve()
    if not str(fp).startswith(str(root.resolve())) or not fp.exists():
        return "not found", 404
    return send_file(fp)


@app.route("/api/preview/<order_no>")
def api_preview(order_no):
    """在线预览订单合订本 PDF（三级目录：客户/日期/订单号）"""
    root = cfgmod.pack_out_dir()
    # 递归找订单号目录
    for fp in root.rglob(f"{order_no}/订单质检报告合订本.pdf"):
        if fp.exists():
            return send_file(fp, mimetype="application/pdf")
    return "not found", 404


# ---------- 异常明细 ----------
@app.route("/api/issues")
def api_issues():
    """缺报告/过期的商品明细。优先从本地索引 CSV 读（快），飞书作为补充。"""
    status_filter = request.args.get("status", "")  # missing | expired | ""

    # 从本地所有 00_索引.csv 扫描
    import csv
    lib = cfgmod.library_root()
    packed = lib / "_packed"
    items = []

    if packed.exists():
        for idx_csv in packed.rglob("*/**/00_索引.csv"):
            # 从路径解析客户和日期: 客户名/日期/订单号/00_索引.csv
            parts = idx_csv.parts
            order_dir = idx_csv.parent
            date_dir = order_dir.parent
            customer_dir = date_dir.parent
            customer = customer_dir.name if customer_dir != packed else ""
            delivery_date = date_dir.name if date_dir != packed else ""
            order_no = order_dir.name

            try:
                with open(idx_csv, encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        status = row.get("状态", "")
                        product = row.get("商品", "")
                        report = row.get("报告文件", "")

                        if status_filter == "missing" and "无报告" not in status:
                            continue
                        if status_filter == "expired" and "过期" not in status:
                            continue
                        if not status_filter and "正常" in status:
                            continue

                        items.append({
                            "order_no": order_no,
                            "customer": customer,
                            "delivery_date": delivery_date,
                            "product": product,
                            "status": status,
                            "report": report,
                        })
            except Exception:
                continue

    return jsonify({"total": len(items), "items": items[:500]})


# ---------- 打开本地文件夹 ----------
@app.route("/api/open-folder")
def api_open_folder():
    """调用系统 open 命令打开本地文件夹（macOS/Windows）"""
    rel = request.args.get("path", "").strip()
    lib = cfgmod.library_root()
    if not rel:
        target = lib / "_packed"
    else:
        target = (lib / rel).resolve()
        # 安全检查：必须在 library 根目录下
        if not str(target).startswith(str(lib.resolve())):
            return jsonify({"ok": False, "error": "路径越界"}), 400
    if not target.exists():
        return jsonify({"ok": False, "error": f"目录不存在: {target}"}), 404
    try:
        subprocess.Popen(["open", str(target)])
        return jsonify({"ok": True, "path": str(target)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def run(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args(argv)
    host = a.host or cfgmod.get("server", "host", default="127.0.0.1")
    port = a.port or int(cfgmod.get("server", "port", default=8765))
    cfgmod.ensure_dirs()
    print(f"qc-report-keeper 工作台: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run()
