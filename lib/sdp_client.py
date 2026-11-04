#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蔬东坡开放平台 V2 客户端（纯标准库，无第三方依赖）

用法：
  export SDP_APPID=e31649be1793b380
  export SDP_SECRET=644ff72ecc291cee488146af465d7ce7
  export SDP_PROJECT_CODE=cc_xdsl            # 已实测可用的客户正式环境（默认值）
  export SDP_SITE_ID=6                       # site_id 已在 token 的 JWT claim 里固定为 6

  python3 sdp_client.py doctor                 # 连通性自检
  python3 sdp_client.py sites                  # 拿站点列表
  python3 sdp_client.py orders --date 2026-11-02
  python3 sdp_client.py order --no DD25xxxxxxxx
  python3 sdp_client.py trace --code SPZS202607240001
  python3 sdp_client.py scan --date 2026-11-02 # 一整套：订单→溯源码→质检报告URL

关键结论（来自官方文档）：
  1. 鉴权 = appid + secret 换 access_token（24h），Header 带 Authorization: Bearer xxx
  2. 所有业务接口的根路径是 https://scm.sdongpo.com/cc_<客户项目code>
     —— 测试环境 cc_thirdparty 只认蔬东坡自己的测试凭证，客户凭证在这儿必然 403
  3. 质检报告没有独立接口，只有溯源链路：
     订单详情 commodity_list[].cloud_origin_url（含 origin_code）
       → GET /openApiV2/commodity/getCommodityTraceList   (或 sdpcloud 的 wap 版)
       → trace_info[].trace_report_arr（图片）/ trace_report_pdf（PDF）
       → production_info[].organization_certificate_attach（厂商质检报告）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "https://scm.sdongpo.com"
WAP_HOST = "https://sdpcloud.sdongpo.com"
TOKEN_CACHE = Path(os.environ.get("SDP_TOKEN_CACHE", Path.home() / ".sdp_token.json"))


class SdpError(RuntimeError):
    def __init__(self, path, status, message, raw=None):
        super().__init__(f"[{path}] http={status} {message}")
        self.path = path
        self.status = status
        self.message = message
        self.raw = raw


class SdpClient:
    def __init__(self, appid=None, secret=None, project_code=None, site_id=None,
                 timeout=30, verbose=False):
        self.appid = appid or os.environ.get("SDP_APPID", "")
        self.secret = secret or os.environ.get("SDP_SECRET", "")
        # 客户正式环境项目 code（已实测：cc_xdsl）
        self.project_code = project_code or os.environ.get("SDP_PROJECT_CODE") or "cc_xdsl"
        sid = site_id if site_id is not None else os.environ.get("SDP_SITE_ID")
        self.site_id = int(sid) if sid else None
        self.timeout = timeout
        self.verbose = verbose
        self._last_call = {}          # path -> 上次调用时间，用于客户端限流
        self._min_interval = 0.25     # 默认 4 QPS；auth 用 0.34

    # ---------------- 基础设施 ----------------

    @property
    def base(self):
        if not self.project_code:
            raise SystemExit(
                "缺少项目 code。正式环境地址是 https://scm.sdongpo.com/cc_<项目code>，\n"
                "这不是 appid，需要找蔬东坡商务/客户成功要，例如 cc_lvyikang。\n"
                "拿到后 export SDP_PROJECT_CODE=cc_xxx"
            )
        return f"{HOST}/{self.project_code}"

    @staticmethod
    def _request_id():
        return hashlib.md5(f"{time.time()}{random.random()}".encode()).hexdigest()

    def _throttle(self, path, min_interval=None):
        gap = min_interval or self._min_interval
        last = self._last_call.get(path, 0)
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[path] = time.time()

    def _http(self, method, url, headers=None, body=None, retries=3):
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "User-Agent": "sdp-client/1.0"}
        hdrs.update(headers or {})
        last_err = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read().decode("utf-8", "replace")
                if self.verbose:
                    print(f"  ← {resp.status} {url}", file=sys.stderr)
                return json.loads(payload) if payload.strip() else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 ** attempt)
                    last_err = SdpError(url, e.code, raw[:300], raw)
                    continue
                try:
                    parsed = json.loads(raw)
                    msg = parsed.get("message") or raw[:300]
                except Exception:
                    msg = raw[:300]
                raise SdpError(url, e.code, msg, raw)
            except urllib.error.URLError as e:
                last_err = SdpError(url, 0, str(e.reason))
                if attempt < retries - 1:
                    time.sleep(1.5 ** attempt)
                    continue
                raise last_err
        raise last_err

    # ---------------- 鉴权 ----------------

    def _load_cache(self):
        try:
            return json.loads(TOKEN_CACHE.read_text())
        except Exception:
            return {}

    def _save_cache(self, cache):
        try:
            TOKEN_CACHE.write_text(json.dumps(cache, indent=2))
            TOKEN_CACHE.chmod(0o600)
        except Exception:
            pass

    def get_token(self, force=False):
        if not self.appid or not self.secret:
            raise SystemExit("缺少 SDP_APPID / SDP_SECRET")
        key = f"{self.appid}:{self.site_id}"
        cache = self._load_cache()
        hit = cache.get(key)
        if hit and not force and hit.get("expire_at", 0) - 120 > time.time():
            return hit["token"]

        self._throttle("auth", 0.34)
        url = f"{self.base}/openApiV2/Auth/getToken"
        body = {"appid": self.appid, "secret": self.secret, "site_id": self.site_id}
        res = self._http("POST", url, {"X-Request-Id": self._request_id()}, body)
        if res.get("status") != 1:
            # 常见：appid未注册（凭证与环境下不匹配）/ secret错误 / 未开通openapi
            raise SdpError("getToken", 403, res.get("message") or str(res))
        data = res["data"]
        cache[key] = {"token": data["access_token"],
                      "expire_at": data.get("token_expired_at", time.time() + 86000)}
        self._save_cache(cache)
        return data["access_token"]

    def _get(self, path, params=None, host=None, auth=True, qps=5):
        self._throttle(path, 1.0 / qps if qps else None)
        token = self.get_token() if auth else None
        qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{host or self.base}{path}" + (f"?{qs}" if qs else "")
        headers = {"X-Request-Id": self._request_id()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return self._http("GET", url, headers)
        except SdpError as e:
            if e.status == 401 and auth:
                self.get_token(force=True)
                headers["Authorization"] = f"Bearer {self.get_token()}"
                return self._http("GET", url, headers)
            raise

    def _post(self, path, body, qps=5):
        self._throttle(path, 1.0 / qps if qps else None)
        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.get_token()}",
                   "X-Request-Id": self._request_id()}
        return self._http("POST", url, headers, body)

    # ---------------- 业务接口 ----------------

    def site_list(self):
        """GET /openApiV2/common/siteList  获得全部站点"""
        return self._get("/openApiV2/common/siteList", qps=5).get("data") or []

    def order_page_list(self, start_delivery_date=None, end_delivery_date=None,
                        start_create_time=None, end_create_time=None,
                        status=None, order_no=None, page=1, page_size=50):
        """GET /openApiV2/Order/pageList  qps5，时间跨度最大 93 天"""
        params = {"start_delivery_date": start_delivery_date,
                  "end_delivery_date": end_delivery_date,
                  "start_create_time": start_create_time,
                  "end_create_time": end_create_time,
                  "status": status, "order_no": order_no,
                  "page": page, "page_size": page_size}
        return self._get("/openApiV2/Order/pageList", params, qps=5)

    def iter_orders(self, start_delivery_date=None, end_delivery_date=None,
                    status=None, page_size=50, max_pages=200):
        for page in range(1, max_pages + 1):
            res = self.order_page_list(start_delivery_date=start_delivery_date,
                                       end_delivery_date=end_delivery_date,
                                       status=status, page=page, page_size=page_size)
            data = res.get("data") or {}
            rows = data.get("list") or data.get("data") or []
            if not rows:
                return
            for r in rows:
                yield r
            if len(rows) < page_size:
                return

    def order_detail(self, order_id=None, order_no=None):
        """GET /openApiV2/Order/detail  qps5（cloud_origin_url 需 17.1.0+）"""
        if not order_id and not order_no:
            raise ValueError("order_id / order_no 至少给一个")
        res = self._get("/openApiV2/Order/detail",
                        {"order_id": order_id, "order_no": order_no}, qps=5)
        return res.get("data") or {}

    def commodity_trace(self, origin_code):
        """GET /openApiV2/commodity/getCommodityTraceList  qps20
        返回 trace_info（含质检报告图片/PDF）、production_info（厂商资质+质检报告）"""
        res = self._get("/openApiV2/commodity/getCommodityTraceList",
                        {"origin_code": origin_code}, qps=20)
        return res.get("data") or {}

    def wap_trace(self, origin_code):
        """GET https://sdpcloud.sdongpo.com/wap/commodity/get-commodity-source
        溯源 H5 用的接口，字段更全（含 expire_date / validity_duration），不需要 token"""
        res = self._get("/wap/commodity/get-commodity-source",
                        {"origin_code": origin_code}, host=WAP_HOST, auth=False, qps=5)
        if str(res.get("success")).lower() == "false":
            raise SdpError("wapTrace", 200, res.get("msg") or str(res), res)
        return res.get("data") or {}

    def download(self, url, dest_dir, filename=None):
        """下载质检报告文件（PDF/图片）"""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = filename or os.path.basename(urllib.parse.urlparse(url).path) or f"file_{int(time.time())}"
        name = re.sub(r"[^\w.\-]", "_", name)
        dest = dest_dir / name
        req = urllib.request.Request(url, headers={"User-Agent": "sdp-client/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            dest.write_bytes(r.read())
        return dest

    # ---------------- 数据整形 ----------------

    @staticmethod
    def parse_origin_code(url_or_code):
        """cloud_origin_url 里抠 origin_code；传入的就是 code 也直接返回"""
        if not url_or_code:
            return None
        m = re.search(r"origin_code=([A-Za-z0-9]+)", str(url_or_code))
        if m:
            return m.group(1)
        s = str(url_or_code).strip()
        return s if re.fullmatch(r"[A-Za-z0-9]{6,40}", s) else None

    @staticmethod
    def extract_reports(trace_data):
        """从溯源数据里抽出全部质检/检测报告文件"""
        out = []
        for item in trace_data.get("trace_info") or []:
            for f in item.get("trace_report_arr") or []:
                out.append({"kind": "report_image",
                            "name": f.get("origin_file_name"),
                            "url": f.get("report_pic"),
                            "category": f.get("attachment_category_name"),
                            "expire_date": f.get("expire_date"),
                            "inspect_date": item.get("inspect_date"),
                            "inspect_org": item.get("inspect_organization") or item.get("inspect_from"),
                            "batch": item.get("oc_batch_no") or item.get("batch_no"),
                            "matching_mode": item.get("matching_mode")})
            for f in item.get("trace_report_pdf") or []:
                out.append({"kind": "report_pdf",
                            "name": f.get("origin_file_name"),
                            "url": f.get("report_pic"),
                            "category": f.get("attachment_category_name"),
                            "expire_date": f.get("expire_date"),
                            "inspect_date": item.get("inspect_date"),
                            "inspect_org": item.get("inspect_organization") or item.get("inspect_from"),
                            "batch": item.get("oc_batch_no") or item.get("batch_no"),
                            "matching_mode": item.get("matching_mode")})
        for p in trace_data.get("production_info") or []:
            atts = p.get("organization_certificate_attach") or []
            if isinstance(atts, str):
                atts = [atts]
            for u in atts:
                out.append({"kind": "factory_qc_report",
                            "name": os.path.basename(str(u)),
                            "url": u,
                            "batch": p.get("oc_batch_no"),
                            "production_date": p.get("production_date"),
                            "inspect_org": p.get("organization_name")})
        return [r for r in out if r.get("url")]


# ---------------- 自检 ----------------

def _jwt_claims(token: str) -> dict:
    """不验签，只解 payload——用于读 site_id / client_id。"""
    import base64
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


PERM_MATRIX = [
    ("/openApiV2/common/siteList", "站点列表", {}),
    ("/openApiV2/Order/pageList", "订单列表", {"page": 1, "page_size": 1,
                                              "start_date": "2026-11-02", "end_date": "2026-11-03"}),
    ("/openApiV2/Order/detail", "订单详情（关键：带 cloud_origin_url）", None),
    ("/openApiV2/Commodity/pageList", "商品列表", {"page": 1, "page_size": 1}),
    ("/openApiV2/commodity/getCommodityTraceList", "商品溯源（关键：质检报告）", None),
]


def doctor(cli: "SdpClient"):
    print(f"环境地址 : {cli.base}")
    print(f"appid    : {cli.appid}")
    print(f"site_id  : {cli.site_id}（请求参数）")
    t = cli.get_token(force=True)
    claims = _jwt_claims(t).get("claim") or {}
    print(f"token    : {t[:32]}...  (OK)")
    print(f"JWT claim: site_id={claims.get('site_id')} client_id={claims.get('client_id')} "
          f"auth_account_id={claims.get('auth_account_id')} allowed={claims.get('is_allowed')}")
    print()

    # 先取一个真实订单号 + 溯源码，供后面两个接口用
    order_no = origin_code = None
    try:
        res = cli.order_page_list(start_delivery_date="2026-11-02",
                                  end_delivery_date="2026-11-03", page_size=5)
        lst = (res.get("data") or {}).get("list") or []
        if lst:
            order_no = lst[0].get("order_no")
            det = cli.order_detail(order_no=order_no)
            for c in det.get("commodity_list") or []:
                oc = SdpClient.parse_origin_code(c.get("cloud_origin_url"))
                if oc:
                    origin_code = oc
                    break
    except SdpError:
        pass

    print(f"{'接口':<48}{'用途':<30}{'结果'}")
    print("-" * 96)
    ok = 0
    for path, desc, params in PERM_MATRIX:
        if path.endswith("/Order/detail"):
            if not order_no:
                print(f"{path:<48}{desc:<30}跳过（无样例订单）")
                continue
            params = {"order_no": order_no}
        elif path.endswith("getCommodityTraceList"):
            if not origin_code:
                print(f"{path:<48}{desc:<30}跳过（样例订单无溯源码）")
                continue
            params = {"origin_code": origin_code}
        try:
            res = cli._get(path, params, qps=5)
            n = "-"
            d = res.get("data")
            if isinstance(d, list):
                n = f"{len(d)} 条"
            elif isinstance(d, dict):
                if "list" in d:
                    n = f"{len(d.get('list') or [])} 条"
                else:
                    n = f"{len(d)} 字段"
            print(f"{path:<48}{desc:<30}✅ {n}")
            ok += 1
        except SdpError as e:
            mark = "❌" if e.status == 451 else f"⚠️HTTP{e.status}"
            print(f"{path:<48}{desc:<30}{mark} {e.message[:30]}")
    print("-" * 96)
    print(f"可用 {ok}/{len(PERM_MATRIX)}  · 样例订单 {order_no or '-'}  · 样例溯源码 {origin_code or '-'}")
    if ok < 3:
        print("\n提示：451「无访问接口权限」是接口级授权，需让蔬东坡在后台给这个 appid 勾选对应接口。")


# ---------------- 探测器 ----------------

def probe(appid, secret, project_code=None, site_ids=None):
    """凭证/环境矩阵探测：确认 appid 到底有没有注册、注册在哪个环境。

    背景：蔬东坡的 "appid未注册" 是全局校验，跟项目 code 无关——
    https://scm.sdongpo.com/openApiV2/Auth/getToken 不带项目 code 也照样报未注册。
    所以先跑这个矩阵，比反复猜项目 code 有用。
    """
    import urllib.request

    site_ids = site_ids or list(range(0, 7))
    roots = ["", "cc_thirdparty"]
    if project_code:
        roots.append(project_code if project_code.startswith("cc_") else f"cc_{project_code}")

    def call(url, body=None, method="POST"):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Content-Type": "application/json",
                "X-Request-Id": hashlib.md5(f"{time.time()}{random.random()}".encode()).hexdigest(),
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode("utf-8", "replace")
            if txt.lstrip().startswith("{"):
                d = json.loads(txt)
                return ("TOKEN_OK" if d.get("status") == 1 else "ERR"), (d.get("message") or "")[:60]
            return "HTML", "非 JSON（路径不存在）"
        except urllib.error.HTTPError as e:
            return f"HTTP{e.code}", e.read().decode("utf-8", "replace")[:60]
        except Exception as e:
            return "FAIL", str(e)[:60]

    print(f"appid = {appid}\nsecret = {secret[:6]}…{secret[-4:]} (len={len(secret)})\n")
    print(f"{'环境根路径':<28}{'site_id':<9}{'V2 getToken':<14}{'返回信息'}")
    print("-" * 96)
    hit = []
    for root in roots:
        base = f"{HOST}/{root}" if root else HOST
        for sid in (site_ids if root else [None]):
            st, msg = call(f"{base}/openApiV2/Auth/getToken",
                           {"appid": appid, "secret": secret, "site_id": sid})
            print(f"{('/' + root) or '/(无项目code)':<28}{str(sid):<9}{st:<14}{msg}")
            if st == "TOKEN_OK":
                hit.append((root, sid))
    print("-" * 96)
    st, msg = call(f"{HOST}/openApi/commodity/list?appid={appid}"
                   f"&timestamp={int(time.time())}&sign=0123456789abcdef0123456789abcdef", method="GET")
    print(f"V1 老接口 /openApi/commodity/list → {st} {msg}")

    print()
    if hit:
        print(f"✅ 命中：项目 code = {hit[0][0]!r}，site_id = {hit[0][1]}，可以直接跑 doctor")
    elif "未注册" in msg or any("未注册" in m for _, m in [("", msg)]):
        print("❌ 判定：这对 appid/secret 在蔬东坡开放平台（V1 和 V2 都试了）里查不到注册记录。")
        print("   跟项目 code 无关——不带项目 code 的根路径也报未注册。")
        print("   可能原因：")
        print("   1) 这对凭证不是 openapi 的，是其他模块的授权（溯源/一物一码/小程序/第三方对接页）")
        print("   2) openapi 服务还没真正开通或凭证未生效（FAQ 里的『未开通 openapi 服务』）")
        print("   3) 凭证属于另一套部署（私有化/独立域名），不在 scm.sdongpo.com 上")
        print("   → 拿这句原话去问：『appid e31649be1793b380 在开放平台是否已注册？对应项目 code 和 site_id 是多少？』")
    else:
        print("⚠️ 全部失败但不是未注册，看上面返回信息逐条排查。")
    return hit


# ---------------- CLI ----------------

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="蔬东坡开放平台 V2 客户端")
    ap.add_argument("--project-code")
    ap.add_argument("--site-id", type=int)
    ap.add_argument("--appid")
    ap.add_argument("--secret")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="连通性自检")
    sub.add_parser("probe", help="凭证/环境矩阵探测（先跑这个）")
    sub.add_parser("sites", help="站点列表")

    p = sub.add_parser("orders", help="按发货日期拉订单")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("order", help="订单详情")
    p.add_argument("--no", dest="order_no")
    p.add_argument("--id", dest="order_id")

    p = sub.add_parser("trace", help="按溯源码查质检报告")
    p.add_argument("--code", required=True)

    p = sub.add_parser("wap", help="公开溯源接口（免鉴权，不要 appid 也能用）")
    p.add_argument("--code", required=True)

    p = sub.add_parser("scan", help="订单→溯源码→质检报告，一套跑通")
    p.add_argument("--date", required=True)
    p.add_argument("--limit", type=int, default=5)

    a = ap.parse_args()
    cli = SdpClient(a.appid, a.secret, a.project_code, a.site_id, verbose=a.verbose)

    try:
        if a.cmd == "probe":
            probe(cli.appid, cli.secret, a.project_code)

        elif a.cmd == "doctor":
            doctor(cli)

        elif a.cmd == "sites":
            _print(cli.site_list())

        elif a.cmd == "orders":
            rows = list(cli.iter_orders(start_delivery_date=a.date,
                                        end_delivery_date=a.date, status=a.status))
            print(f"共 {len(rows)} 单")
            for r in rows[:a.limit]:
                print(f"  {r.get('order_no')}  id={r.get('order_id') or r.get('id')}  "
                      f"状态={r.get('status_desc') or r.get('status')}  "
                      f"客户={r.get('user_name') or r.get('user_code')}")

        elif a.cmd == "order":
            d = cli.order_detail(a.order_id, a.order_no)
            order = d.get("order") or {}
            print(f"订单 {order.get('order_no')}  状态={order.get('status_desc')}")
            for c in d.get("commodity_list") or []:
                code = SdpClient.parse_origin_code(c.get("cloud_origin_url"))
                print(f"  - {c.get('name')} x{c.get('delivery_amount') or c.get('amount')}"
                      f"{c.get('unit')}  溯源码={code or '无'}")

        elif a.cmd == "wap":
            d = cli.wap_trace(a.code)
            info = d.get("com_info") or d.get("commodity_info") or {}
            print(f"商品：{info.get('name')}  品牌={info.get('brand')}  产地={info.get('product_place')}")
            reps = SdpClient.extract_reports(d)
            print(f"报告文件 {len(reps)} 个：")
            for r in reps:
                print(f"  [{r['kind']}] {r['name']}  检测日期={r.get('inspect_date')} "
                      f"有效期={r.get('expire_date')}\n      {r['url']}")

        elif a.cmd == "trace":
            d = cli.commodity_trace(a.code)
            info = d.get("commodity_info") or {}
            print(f"商品：{info.get('name')}  品牌={info.get('brand')}  产地={info.get('product_place')}")
            reps = SdpClient.extract_reports(d)
            print(f"报告文件 {len(reps)} 个：")
            for r in reps:
                print(f"  [{r['kind']}] {r['name']}  检测日期={r.get('inspect_date')} "
                      f"机构={r.get('inspect_org')}\n      {r['url']}")
            for ti in d.get("trace_info") or []:
                print(f"检测记录：{ti.get('inspect_date')} 机构={ti.get('inspect_organization')} "
                      f"匹配模式={ti.get('matching_mode')} 抑制率={(ti.get('trace_value') or {}).get('restrain_rate')}")

        elif a.cmd == "scan":
            rows = list(cli.iter_orders(start_delivery_date=a.date, end_delivery_date=a.date))
            print(f"{a.date} 共 {len(rows)} 单，抽查前 {a.limit} 单")
            for r in rows[:a.limit]:
                no = r.get("order_no")
                try:
                    d = cli.order_detail(order_no=no)
                except SdpError as e:
                    print(f"  {no} 详情失败：{e.message}")
                    continue
                for c in d.get("commodity_list") or []:
                    code = SdpClient.parse_origin_code(c.get("cloud_origin_url"))
                    if not code:
                        print(f"  {no} / {c.get('name')} → 无溯源码（无质检报告）")
                        continue
                    try:
                        t = cli.commodity_trace(code)
                        reps = SdpClient.extract_reports(t)
                        print(f"  {no} / {c.get('name')} → {code} → 报告 {len(reps)} 份")
                    except SdpError as e:
                        print(f"  {no} / {c.get('name')} → {code} 拉取失败：{e.message}")

    except SdpError as e:
        print(f"调用失败：{e}", file=sys.stderr)
        if "未注册" in str(e.message):
            print("\n判定：这个 appid 不属于该环境。\n"
                  "  正式环境 = https://scm.sdongpo.com/cc_<客户项目code>\n"
                  "  需要向蔬东坡要：项目 code + site_id，并把凭证对应到该环境。", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
