#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
供应商报告入库：把散乱的图片/PDF 自动识别、定性、改名、归档

解决的问题：供应商通过微信/邮件发来一堆 `微信图片_2026xxx.jpg`、`扫描件.pdf`，
混着三种完全不同的东西——第三方快检单、厂家出厂检验报告、资质证件。
不分清楚，出了事拿不出来，也说不清哪份能对外。

核心不是"存文件"，是按**法律效力**分三级：

  L1 法定检验  — 带 CMA/CNAS 标志的检验检测报告，可对外举证
  L2 出厂检验  — 生产企业自检，证明"厂家检过"，不等于第三方合格
  L3 快检筛查  — 快检单/酶抑制率法，企业内部质控用，不能对外
  L0 资质证件  — 营业执照、食品生产/经营许可证、检验报告封面等

用法：
  python3 qc_ingest.py --inbox ~/Downloads/供应商报告 --library ~/qc_library
  python3 qc_ingest.py --inbox ... --dry-run     # 只看识别结果，不动文件

输出：
  library/
    L1_法定检验/2026-11/华测_果蔬类快检_20261102.pdf
    L2_出厂检验/2026-10/禾一豆制品坊_老豆腐_20261021.png
    L0_资质证件/...
    _index.csv                    全部文件一行一条
    _review/                      识别不确定的放这里，等人工确认
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent.resolve()
# qc_ingest.py 在 lib/ 下，tools/ocr 在仓库根目录
OCR_BIN = HERE.parent / "tools" / "ocr"
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
PDF_EXT = {".pdf"}


# ---------------- 文本提取 ----------------

def pdf_text(path: Path) -> str:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        parts = []
        for i in range(min(len(pdf), 20)):
            try:
                parts.append(pdf[i].get_textpage().get_text_range())
            except Exception:
                pass
        return "\n".join(parts)
    except Exception as e:
        print(f"    PDF 文本提取失败 {path.name}: {e}", file=sys.stderr)
        return ""


def image_text(path: Path) -> str:
    if not OCR_BIN.exists():
        return ""
    try:
        r = subprocess.run([str(OCR_BIN), str(path)], capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    except Exception as e:
        print(f"    OCR 失败 {path.name}: {e}", file=sys.stderr)
        return ""


def to_image(path: Path, tmpdir: Path) -> Path | None:
    """PDF 转首图，供 OCR 用"""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        if len(pdf) == 0:
            return None
        out = tmpdir / (path.stem[:40] + "_p1.png")
        pdf[0].render(scale=2.0).to_pil().save(out)
        return out
    except Exception:
        return None


# ---------------- 识别规则 ----------------

LEVEL_RULES = [
    # (正则, 等级, 类型标签)  —— 顺序有讲究：先判最具体的
    (r"快速检测|快检|酶抑制|分光光度法", "L3", "快检筛查"),
    (r"出厂检验|出厂检验报告", "L2", "出厂检验"),
    (r"检验检测报告|检测报告|检验报告", "L1", "法定检验"),
    (r"营业执照|生产许可|经营许可|食品生产许可证|食品经营许可证|SC\d{14}", "L0", "资质证件"),
]

ITEM_RULES = [
    ("农残快检", r"有机磷|氨基甲酸酯|农药残留|农残"),
    ("感官指标", r"形态|气味|滋味|色泽|组织|杂质|口感"),
    ("微生物", r"菌落总数|大肠菌群|沙门氏|金黄色葡萄球菌|霉菌|酵母"),
    ("理化", r"酸价|过氧化值|水分|蛋白质|脂肪|亚硝酸盐|二氧化硫"),
    ("重金属", r"铅|镉|汞|砷|铬|重金属"),
    ("食品添加剂", r"苯甲酸|山梨酸|甜蜜素|糖精钠|脱氢乙酸"),
]

ORG_PATTERNS = [
    r"([\u4e00-\u9fa5]{2,20}?(?:检测技术|检验检测|质量检验|检测|质检)[\u4e00-\u9fa5]{0,6}?(?:有限公司|中心|院|所))",
    r"([\u4e00-\u9fa5]{2,20}?(?:食品|豆制品|农产品|供应链|配送|农业)[\u4e00-\u9fa5]{0,6}?(?:有限公司|厂|坊|合作社|商行))",
]

DATE_PATTERNS = [
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
]

PRODUCT_HINTS = [
    "豆腐", "豆干", "豆皮", "腐竹", "蔬菜", "果蔬", "猪肉", "牛肉", "鸡肉", "鸡蛋",
    "大米", "面粉", "食用油", "酱油", "醋", "盐", "奶", "面粉", "粉丝", "木耳",
]


def find_dates(text: str) -> list[str]:
    out = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            # 合法性校验：OCR 常把"年"附近的字认成数字，月份 87 这种要丢掉
            if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            out.append(f"{y:04d}-{mo:02d}-{d:02d}")
    return sorted(set(out))


def find_org(text: str) -> str:
    for pat in ORG_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def find_product(text: str, fallback_name: str) -> str:
    for kw in PRODUCT_HINTS:
        if kw in text:
            # 尝试抓关键词附近的名词
            for line in text.splitlines():
                if kw in line and len(line) < 30:
                    return line.strip(" |　\t")
            return kw
    base = re.sub(r"[_\-\d.]", " ", fallback_name).strip()
    return base[:20] or "未识别产品"


def classify(text: str) -> dict:
    level, label = "", ""
    for pat, lv, lb in LEVEL_RULES:
        if re.search(pat, text):
            level, label = lv, lb
            break
    if not level:
        level, label = "L?", "未识别"

    items = [name for name, pat in ITEM_RULES if re.search(pat, text)]
    has_cma = bool(re.search(r"CMA|CNAS|MA\s*号|资质认定", text, re.I))
    has_seal_hint = bool(re.search(r"专用章|盖章|签章|公司签章|检测专用章", text))

    # 快检单只要出现"快速检测"就一定是 L3，即使同时有"检验报告"字样
    if "快速检测" in text:
        level, label = "L3", "快检筛查"

    return {
        "level": level,
        "type": label,
        "items": items,
        "has_cma": has_cma,
        "has_seal_hint": has_seal_hint,
    }


def confidence_of(level: str, org: str, dates: list, text: str) -> tuple[float, list[str]]:
    score, why = 0.0, []
    if level != "L?":
        score += 0.4
    else:
        why.append("未识别报告类型")
    if org:
        score += 0.2
    else:
        why.append("未识别机构")
    if dates:
        score += 0.2
    else:
        why.append("未识别日期")
    if len(text) > 40:
        score += 0.2
    else:
        why.append("文本过少")
    return round(score, 2), why


# ---------------- 主流程 ----------------

def file_hash(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:10]


def safe(s: str, n=40) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "", (s or "").strip())
    return s[:n] or "未命名"


def main():
    import config as cfgmod
    ap = argparse.ArgumentParser(description="供应商报告入库")
    ap.add_argument("--inbox", default=None, help="待处理目录，默认从 config.yaml 读")
    ap.add_argument("--library", default=None, help="归档目录，默认从 config.yaml 读")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-confidence", type=float, default=0.6)
    a = ap.parse_args()

    inbox = Path(a.inbox).expanduser() if a.inbox else cfgmod.inbox_dir()
    lib = Path(a.library).expanduser() if a.library else cfgmod.library_root()
    cfgmod.ensure_dirs()
    if not inbox.exists():
        sys.exit(f"目录不存在: {inbox}")

    files = [p for p in inbox.rglob("*")
             if p.is_file() and p.suffix.lower() in (IMG_EXT | PDF_EXT)]
    print(f"待处理 {len(files)} 个文件\n")

    tmp = lib / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    records = []

    for i, p in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {p.name}", file=sys.stderr)
        if p.suffix.lower() in PDF_EXT:
            text = pdf_text(p)
            if len(text.strip()) < 40:          # 扫描版 PDF，走 OCR
                img = to_image(p, tmp)
                if img:
                    text = image_text(img)
                    try:
                        img.unlink()
                    except Exception:
                        pass
        else:
            text = image_text(p)

        cls = classify(text)
        rec = {
            "源文件": p.name,
            "路径": str(p),
            "大小KB": round(p.stat().st_size / 1024),
            "报告等级": cls["level"],
            "报告类型": cls["type"],
            "检测项目": "/".join(cls["items"]),
            "有CMA标志": "是" if cls["has_cma"] else "",
            "有章迹描述": "是" if cls["has_seal_hint"] else "",
            "机构": find_org(text),
            "日期": (find_dates(text) or [""])[-1],
            "产品": find_product(text, p.stem),
            "文本长度": len(text),
            "哈希": file_hash(p),
            "归档路径": "",
            "待确认": "",
        }
        conf, why = confidence_of(rec["报告等级"], rec["机构"], find_dates(text), text)
        rec["置信度"] = conf
        if conf < a.min_confidence:
            rec["待确认"] = ";".join(why)

        # ---- 目标路径 ----
        if rec["待确认"]:
            dest = lib / "_review" / p.name
        else:
            d = rec["日期"] or datetime.now().strftime("%Y-%m-%d")
            sub = f"{d[:7]}"
            fn = f"{safe(rec['机构'] or '未知机构', 16)}_{safe(rec['产品'], 16)}_{d.replace('-', '')}{p.suffix.lower()}"
            dest = lib / f"{rec['报告等级']}_{rec['报告类型']}" / sub / fn

        rec["归档路径"] = str(dest.relative_to(lib)) if lib in dest.parents or dest.parent == lib else str(dest)

        if not a.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_stem(dest.stem + "_" + rec["哈希"])
                rec["归档路径"] = str(dest.relative_to(lib))
            shutil.copy2(p, dest)

        records.append(rec)
        print(f"    {rec['报告等级']} {rec['报告类型']:<8} 机构={rec['机构'] or '-':<18} "
              f"产品={rec['产品']:<12} 日期={rec['日期'] or '-':<12} 置信={conf}"
              + (f"  ⚠{rec['待确认']}" if rec["待确认"] else ""))

    # ---- 索引 ----
    if not a.dry_run and records:
        idx = lib / "_index.csv"
        exists = idx.exists()
        with open(idx, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            if not exists:
                w.writeheader()
            w.writerows(records)

        # ---- 同步到飞书多维表格 ----
        try:
            import bitable
            bitable.upsert_reports(records)
            print(f"✓ 已同步 {len(records)} 条到飞书多维表格")
        except Exception as e:
            print(f"⚠ 多维表格同步失败（不影响本地归档）: {e}", file=sys.stderr)

    # ---- 汇总 ----
    from collections import Counter
    c = Counter(f"{r['报告等级']} {r['报告类型']}" for r in records)
    print(f"\n{'='*58}")
    for k, v in sorted(c.items()):
        print(f"  {k:<20} {v} 个")
    rev = sum(1 for r in records if r["待确认"])
    print(f"{'='*58}")
    print(f"共 {len(records)} 个，待人工确认 {rev} 个")
    if a.dry_run:
        print("（dry-run，未落盘）")
    else:
        print(f"归档目录 {lib}/   索引 {lib}/_index.csv")


if __name__ == "__main__":
    main()
