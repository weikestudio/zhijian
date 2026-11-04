#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按订单打包质检报告 → 一个可直接交出去的 PDF 合订本 + 索引表

解决的问题：学校/监管要"这批货的检测报告"，现在得人工在蔬东坡里一个个点开、下载、重命名。
本工具按订单号自动完成：取商品 → 取溯源码 → 取报告 → 去重下载 → 装订成册。

用法：
  # 单订单
  python3 qc_pack.py --order DD261103120159001952

  # 一天的全部订单
  python3 qc_pack.py --date 2026-11-02

  # 一段时间（按发货日期）
  python3 qc_pack.py --start 2026-11-01 --end 2026-11-03

  # 只出清单不下载
  python3 qc_pack.py --date 2026-11-02 --dry-run

输出：
  packages/DD261103120159001952/
    00_索引.csv              一行一个商品：商品/供应商/溯源码/报告/机构/日期/状态
    订单质检报告合订本.pdf    封面 + 目录 + 全部报告（可整份打印/发送）
    reports/*.pdf|png        原始报告文件（去重后）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ---- 复用已有客户端 ----
sys.path.insert(0, str(Path(__file__).parent))
from sdp_client import SdpClient, SdpError  # noqa: E402
import config as cfgmod  # noqa: E402

HERE = Path(__file__).parent
CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def _font(size):
    from PIL import ImageFont
    for p in CJK_FONT_CANDIDATES:
        if not os.path.exists(p):
            continue
        # .ttc 是字体集合，逐个 index 试
        for idx in (0, 1, 2):
            try:
                return ImageFont.truetype(p, size, index=idx)
            except Exception:
                continue
    return ImageFont.load_default()


# ============ 报告文件 ============

def report_files_of(trace_data: dict) -> list[dict]:
    """从 trace 数据里取出全部报告文件，带上检测记录上下文"""
    out = []
    for ti in trace_data.get("trace_info") or []:
        ctx = {
            "inspect_org": ti.get("inspect_organization") or "",
            "inspect_date": ti.get("inspect_date") or "",
            "inspect_from": ti.get("inspect_from") or "",
            "trace_value": ti.get("trace_value") or {},
            "matching_mode": ti.get("matching_mode"),
        }
        for key, kind in (("trace_report_pdf", "pdf"), ("trace_report_arr", "img")):
            for it in ti.get(key) or []:
                url = it.get("report_pic") or ""
                if not url:
                    continue
                out.append({
                    **ctx,
                    "kind": kind,
                    "name": it.get("origin_file_name") or "",
                    "url": url,
                    "expire_date": it.get("expire_date") or "",
                    "create_time": it.get("create_time") or "",
                    "source": "trace",
                })
        if not out or not any(r["source"] == "trace" for r in out):
            u = ti.get("pdf_link") or ti.get("pic_link")
            if u:
                out.append({**ctx, "kind": "pdf" if ti.get("pdf_link") else "img",
                            "name": "", "url": u, "expire_date": "", "create_time": "",
                            "source": "trace"})
        break   # 只用最新一条 trace_info（接口本就只返回最新批次）

    for p in trace_data.get("production_info") or []:
        atts = p.get("organization_certificate_attach") or []
        if isinstance(atts, str):
            atts = [atts]
        for u in atts:
            if u:
                out.append({
                    "inspect_org": p.get("organization_name") or "",
                    "inspect_date": p.get("production_date") or "",
                    "inspect_from": "厂商资质",
                    "trace_value": {},
                    "matching_mode": None,
                    "kind": "factory",
                    "name": os.path.basename(str(u)),
                    "url": str(u),
                    "expire_date": "",
                    "create_time": "",
                    "batch": p.get("oc_batch_no") or "",
                    "source": "production",
                })
    return out


def safe_name(s: str, maxlen=60) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (s or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:maxlen] or "未命名"


def download(url: str, dest: Path, timeout=60) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 报告 URL 里带中文文件名，必须先做百分号编码
    safe_url = urllib.parse.quote(url, safe=":/?&=#%+~")
    try:
        req = urllib.request.Request(safe_url, headers={"User-Agent": "Mozilla/5.0 qc-pack/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"    下载失败 {safe_url[-50:]} : {e}", file=sys.stderr)
        return False


# ============ 合订本 ============

def img_to_pdf_bytes(path: Path) -> bytes:
    from PIL import Image
    im = Image.open(path)
    if im.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def make_alert_page(order: dict, missing: list) -> bytes:
    """极简缺报告提示页：无抬头，只列缺报告的商品"""
    from PIL import Image, ImageDraw
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_title = _font(44)
    f_body = _font(26)
    f_small = _font(22)
    d.text((90, 120), f"订单 {order.get('order_no','')}  未取得报告的商品", font=f_title, fill="#c0392b")
    d.line([90, 200, W - 90, 200], fill="#ddd", width=2)
    y = 260
    for i, m in enumerate(missing[:30], 1):
        d.text((110, y), f"{i}. {m}", font=f_body, fill="#333")
        y += 44
    if len(missing) > 30:
        d.text((110, y), f"… 共 {len(missing)} 项，详见索引表", font=f_small, fill="#999")
    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def make_cover(order: dict, stats: dict) -> bytes:
    """生成 A4 封面"""
    from PIL import Image, ImageDraw
    W, H = 1240, 1754          # A4 @150dpi
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_title = _font(58)
    f_sub = _font(30)
    f_body = _font(26)
    f_small = _font(22)

    d.rectangle([0, 0, W, 16], fill="#1f6feb")
    d.text((90, 150), "学校食堂食材", font=f_sub, fill="#666")
    d.text((90, 200), "食品检测报告合订本", font=f_title, fill="#111")
    d.line([90, 300, W - 90, 300], fill="#ddd", width=3)

    y = 360
    rows = [
        ("订单号", order.get("order_no", "")),
        ("客户", order.get("user_name", "")),
        ("收货地址", (order.get("address_detail") or "")[:40]),
        ("发货日期", order.get("delivery_date", "")),
        ("下单时间", order.get("create_time", "")),
        ("商品行数", str(stats.get("items", 0))),
        ("检测报告", f"{stats.get('reports', 0)} 份（去重后）"),
        ("缺失/异常", f"{stats.get('issues', 0)} 项"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    for k, v in rows:
        d.text((90, y), k, font=f_body, fill="#888")
        d.text((280, y), str(v), font=f_body, fill="#222")
        y += 52

    y += 30
    d.line([90, y, W - 90, y], fill="#eee", width=2)
    y += 34
    d.text((90, y), "报告来源：蔬东坡供应链系统溯源记录", font=f_small, fill="#666")
    d.text((90, y + 36), "本合订本由系统按订单自动装订，报告原件未经修改。", font=f_small, fill="#666")

    # 待确认：匹配到了报告，但名字/供应商对不齐，不能当已验证
    if stats.get("pending"):
        y += 96
        items = stats["pending"]
        h = 46 + 34 * min(len(items), 5)
        d.rectangle([90, y - 12, W - 90, y + h], outline="#d68a1a", width=3)
        d.text((110, y), f"待人工确认：{len(items)} 项匹配到报告但证据未对齐",
               font=f_body, fill="#8a5a00")
        for i, nm in enumerate(items[:5]):
            d.text((130, y + 46 + i * 34), f"· {nm}", font=f_small, fill="#8a5a00")
        y += h + 10

    # 缺失提示
    if stats.get("missing"):
        y += 96
        d.rectangle([90, y - 12, W - 90, y + 46 + 34 * min(len(stats["missing"]), 6)], outline="#e5534b", width=3)
        d.text((110, y), f"注意：以下 {len(stats['missing'])} 个商品未取得有效检测报告", font=f_body, fill="#c0392b")
        for i, nm in enumerate(stats["missing"][:6]):
            d.text((130, y + 46 + i * 34), f"· {nm}", font=f_small, fill="#c0392b")
        if len(stats["missing"]) > 6:
            d.text((130, y + 46 + 6 * 34), f"… 其余 {len(stats['missing']) - 6} 项见索引表",
                   font=f_small, fill="#c0392b")

    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def make_toc(entries: list[dict]) -> bytes:
    """生成目录页，entries: [{idx, commodities, org, date, file, page}]"""
    from PIL import Image, ImageDraw
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_h = _font(40)
    f_body = _font(24)
    f_small = _font(20)

    d.rectangle([0, 0, W, 16], fill="#1f6feb")
    d.text((90, 110), "目录", font=f_h, fill="#111")
    d.line([90, 180, W - 90, 180], fill="#ddd", width=3)

    y = 220
    for e in entries:
        if y > H - 120:
            break
        d.text((90, y), f"P{e['page']}", font=f_body, fill="#1f6feb")
        title = e["commodities"]
        if len(title) > 26:
            title = title[:25] + "…"
        d.text((200, y), title, font=f_body, fill="#222")
        sub = f"{e['org'] or '未标注机构'} · {e['date'] or '无日期'}"
        if len(sub) > 44:
            sub = sub[:43] + "…"
        d.text((200, y + 32), sub, font=f_small, fill="#777")
        y += 84
        d.line([90, y - 18, W - 90, y - 18], fill="#f0f0f0", width=1)
    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def merge_to_pdf(parts: list[bytes], out: Path):
    from pypdf import PdfWriter, PdfReader
    w = PdfWriter()
    for b in parts:
        if not b:
            continue
        try:
            r = PdfReader(io.BytesIO(b))
            for p in r.pages:
                w.add_page(p)
        except Exception as e:
            print(f"    合并跳过一页：{e}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        w.write(f)


# ============ 主流程 ============

def collect_orders(cli, args) -> list[dict]:
    if args.order:
        return [{"order_no": o.strip()} for o in args.order.split(",") if o.strip()]
    start = args.start or args.date
    end = args.end or args.date
    if not start:
        sys.exit("需要 --order 或 --date / --start --end")
    rows, page = [], 1
    while True:
        res = cli.order_page_list(start_delivery_date=start, end_delivery_date=end,
                                  page=page, page_size=100)
        lst = (res.get("data") or {}).get("list") or []
        rows.extend(lst)
        if len(lst) < 100 or page >= 20:
            break
        page += 1
    print(f"{start} ~ {end} 拉到 {len(rows)} 单")
    return rows


def pack_one(cli, order_stub: dict, outroot: Path, dry_run=False) -> dict:
    order_no = order_stub.get("order_no")
    # 单订单模式时 order_stub 只有单号，需要从列表接口补齐客户/地址/日期
    if not order_stub.get("user_name"):
        try:
            res = cli.order_page_list(order_no=order_no, page=1, page_size=20)
            for cand in (res.get("data") or {}).get("list") or []:
                if cand.get("order_no") == order_no:
                    order_stub = {**cand, **order_stub}
                    break
        except SdpError:
            pass

    det = cli.order_detail(order_no=order_no)
    order = {**order_stub, **det.get("order", {})}
    if not order.get("order_no"):
        order["order_no"] = order_no
    items = det.get("commodity_list") or []

    outdir = outroot / safe_name(order_no)
    repdir = outdir / "reports"

    # 1) 逐商品取溯源码 + 报告
    file_registry: dict[str, dict] = {}   # url -> {path, commodities:set, org, date, ...}
    index_rows = []
    missing = []

    for i, c in enumerate(items, 1):
        name = c.get("name", "")
        code = SdpClient.parse_origin_code(c.get("cloud_origin_url", ""))
        rec = {
            "序号": i, "商品": name, "编码": c.get("code", ""),
            "数量": c.get("quantity"), "单位": c.get("unit", ""),
            "供应商": (c.get("provider_name") or "").strip(),
            "溯源码": code or "", "报告文件": "", "检测机构": "",
            "检测日期": "", "报告有效期": "", "状态": "",
        }
        if not code:
            rec["状态"] = "无溯源码"
            missing.append(f"{name}（无溯源码）")
            index_rows.append(rec)
            continue

        try:
            t = cli.commodity_trace(code)
        except SdpError as e:
            rec["状态"] = f"溯源接口失败:{e.message[:20]}"
            missing.append(f"{name}（溯源失败）")
            index_rows.append(rec)
            continue

        files = report_files_of(t)
        if not files:
            rec["状态"] = "无报告"
            rec["检测机构"] = (t.get("trace_info") or [{}])[0].get("inspect_organization") or ""
            missing.append(f"{name}（无报告文件）")
            index_rows.append(rec)
            continue

        main = files[0]
        rec["检测机构"] = main.get("inspect_org", "")
        rec["检测日期"] = main.get("inspect_date", "")
        rec["报告有效期"] = main.get("expire_date", "")

        # 过期判定
        od = (order.get("delivery_date") or order.get("create_time") or "")[:10]
        if main.get("expire_date") and od and main["expire_date"] < od:
            rec["状态"] = f"报告过期({main['expire_date']})"
            missing.append(f"{name}（报告过期 {main['expire_date']}）")
        else:
            rec["状态"] = "正常"

        for f in files:
            url = f["url"]
            if url not in file_registry:
                ext = ".pdf" if ("pdf" in url.lower() or f["kind"] in ("pdf", "factory")) else ".png"
                base = safe_name(f.get("name") or f"{name}_{main.get('inspect_date') or ''}", 50)
                if not base.lower().endswith(ext):
                    base += ext
                path = repdir / base
                # 同名不同 URL 时加短哈希
                if path.exists():
                    path = repdir / f"{path.stem}_{hashlib.md5(url.encode()).hexdigest()[:6]}{ext}"
                file_registry[url] = {
                    "path": path, "commodities": {name}, "org": f.get("inspect_org", ""),
                    "date": f.get("inspect_date", ""), "kind": f["kind"], "url": url,
                    "expire_date": f.get("expire_date", ""),
                }
            else:
                file_registry[url]["commodities"].add(name)

        rec["报告文件"] = "; ".join(os.path.basename(file_registry[f["url"]]["path"]) for f in files)
        index_rows.append(rec)

    # 2) 下载
    if not dry_run:
        for url, meta in file_registry.items():
            ok = download(url, meta["path"])
            if not ok:
                meta["failed"] = True

    # 3) 合订本：原汁原味拼报告原件，不做封面/目录抬头
    stats = {"items": len(items), "reports": len(file_registry),
             "issues": len(missing), "missing": missing}
    if not dry_run:
        outdir.mkdir(parents=True, exist_ok=True)
        parts = []
        # 只有在有缺报告/异常时，才加一页极简提示（不算抬头，是必要告知）
        if missing:
            parts.append(make_alert_page(order, missing))
        for url, meta in file_registry.items():
            if meta.get("failed") or not meta["path"].exists():
                continue
            p = meta["path"]
            if p.suffix.lower() == ".pdf":
                b = p.read_bytes()
            else:
                try:
                    b = img_to_pdf_bytes(p)
                except Exception as e:
                    print(f"    图片转PDF失败 {p.name}: {e}", file=sys.stderr)
                    continue
            parts.append(b)
        merge_to_pdf(parts, outdir / "订单质检报告合订本.pdf")

        # 4) 索引 CSV
        with open(outdir / "00_索引.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(index_rows[0].keys()) if index_rows else ["序号"])
            w.writeheader()
            w.writerows(index_rows)

    status = "✅" if not missing else f"⚠ {len(missing)} 项异常"
    print(f"  {order_no}  商品{len(items)}  报告{len(file_registry)}  {status}")
    for m in missing[:5]:
        print(f"      · {m}")
    if len(missing) > 5:
        print(f"      · …另 {len(missing)-5} 项")
    return {"order_no": order_no, "items": len(items),
            "reports": len(file_registry), "issues": len(missing), "missing": missing}


def main():
    ap = argparse.ArgumentParser(description="按订单打包质检报告")
    ap.add_argument("--order", help="订单号，多个逗号分隔")
    ap.add_argument("--date", help="发货日期 YYYY-MM-DD")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default=None, help="输出根目录，默认从 config.yaml 读")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几单")
    ap.add_argument("--dry-run", action="store_true", help="只取数据，不下载不装订")
    a = ap.parse_args()

    cli = SdpClient()
    orders = collect_orders(cli, a)
    if a.limit:
        orders = orders[: a.limit]
    outroot = Path(a.out) if a.out else cfgmod.pack_out_dir()

    print(f"\n开始打包 {len(orders)} 单 → {outroot}/")
    results = []
    for o in orders:
        try:
            results.append(pack_one(cli, o, outroot, a.dry_run))
        except SdpError as e:
            print(f"  {o.get('order_no')} 失败：{e}", file=sys.stderr)

    tot_i = sum(r["items"] for r in results)
    tot_r = sum(r["reports"] for r in results)
    tot_x = sum(r["issues"] for r in results)
    print(f"\n{'='*60}")
    print(f"订单 {len(results)}  商品行 {tot_i}  报告文件 {tot_r}（含跨订单复用）  异常 {tot_x}")
    print(f"{'='*60}")
    with open(outroot / "_summary.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
