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

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")

# 打包任务状态（简单内存队列）
PACK_JOBS = {}


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# ---------- 仪表盘 ----------
@app.route("/api/stats")
def api_stats():
    s = bitable.stats()
    # 本地归档统计
    lib = cfgmod.library_root()
    file_count = sum(1 for _ in lib.rglob("*") if _.is_file()) if lib.exists() else 0
    return jsonify({
        "reports_total": s.get("reports_total", 0),
        "orders_total": s.get("orders_total", 0),
        "local_files": file_count,
        "library_root": str(lib),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ---------- 报告库 ----------
@app.route("/api/reports")
def api_reports():
    data = bitable.list_reports(page_size=int(request.args.get("limit", 50)))
    return jsonify(data)


# ---------- 订单关联 ----------
@app.route("/api/orders")
def api_orders():
    data = bitable.list_orders(page_size=int(request.args.get("limit", 50)))
    return jsonify(data)


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
        PACK_JOBS[job_id] = {"status": "running", "start": datetime.now().isoformat(), "log": ""}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
            PACK_JOBS[job_id].update({
                "status": "done" if r.returncode == 0 else "failed",
                "log": (r.stdout + "\n" + r.stderr)[-4000:],
                "end": datetime.now().isoformat(),
            })
        except Exception as e:
            PACK_JOBS[job_id].update({"status": "failed", "log": str(e)})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/pack/<job_id>")
def api_pack_status(job_id):
    return jsonify(PACK_JOBS.get(job_id, {"status": "unknown"}))


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
    """列出 _review/ 下待人工确认的报告"""
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
    # 从 _index.csv 匹配待确认原因
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


# ---------- 下载合订本 ----------
@app.route("/api/download/<path:relpath>")
def api_download(relpath):
    root = cfgmod.pack_out_dir()
    fp = (root / relpath).resolve()
    if not str(fp).startswith(str(root.resolve())) or not fp.exists():
        return "not found", 404
    return send_file(fp)


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
