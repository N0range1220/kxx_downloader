#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kxx.moe 纯协议下载器 (browser-free).

用法:
    python kxx_downloader.py https://kxx.moe/c/12347e.htm --epub
    python kxx_downloader.py https://kxx.moe/c/12347e.htm --mobi
    python kxx_downloader.py https://kxx.moe/c/12347e.htm --list   # 只列卷,不下载
    python kxx_downloader.py <url> --epub --workers 4              # 并发下载 (默认开启)
    python kxx_downloader.py <url> --epub --proxy http://127.0.0.1:7890
    python kxx_downloader.py <url> --epub --proxy-file proxies.txt

凭据填写在同目录 accounts.json (会自动切换账号,额度耗尽时切换下一个).
代理格式: http://host:port  或  socks5://host:port  (socks5 需 pip install requests[socks]).
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False


BASE_URL = "https://kxx.moe"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
KM_FROM = "KMOE/1.0"  # 站点 zxcomm.js 自报头,服务端可能校验

# 下载格式 -> getdownurl.php 的 mobi 参数
FMT_CODE = {"mobi": 1, "epub": 2, "zip": 0}

# 错误码 -> 中文 (来自 volcomm.js / login.php / book 页 arr_codeinfo)
LOGIN_ERRORS = {
    "e400": "账号或密码错误",
    "e401": "非法访问,请用浏览器正常打开本站",
    "e402": "账号不存在",
    "e403": "验证失效,请刷新页面重新操作",
    "e404": "错误次数过多,请 24 小时后再试 (或需通过验证码)",
}
DL_ERRORS = {
    "e400": "文档暂不支持下载",
    "e401": "取登录状态失败,请先登录",
    "e402": "权限不足",
    "e403": "Kmoe 网站额度不足",
    "e430": "未勾选卷或额度不足",
    "e491": "验证失败,请刷新页面后重试",
    "e499": "系统错误",
    "vip":  "需 VIP 才可使用 (若是新 VIP 用户请退出重新登录)",
    "lv03": "用户等级不足,需达 Lv3",
    "lv05": "用户等级不足,需达 Lv5",
}


# 全局打印锁 (并发时避免多线程 print 交错)
_PRINT_LOCK = threading.Lock()


def log(msg: str):
    with _PRINT_LOCK:
        print(msg, flush=True)


def strip_kmoe_tag(name: str) -> str:
    """去除 [Kmoe] 站点水印标签 (大小写不敏感, 含尾随空格)."""
    return re.sub(r"\[kmoe\]\s*", "", name, flags=re.IGNORECASE).strip()


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清洗文件名: 去掉非法字符, 限制长度."""
    name = strip_kmoe_tag(name)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


class KxxLoginError(RuntimeError):
    pass


class KxxClient:
    """单个账号的协议客户端 (纯 HTTP, 无浏览器依赖).

    线程安全说明: 单个 KxxClient 内的 requests.Session 在 requests 库中
    并不是完全线程安全的, 因此并发下载时每个 worker 应使用独立的 client;
    quota 状态读写通过 quota_lock 保护.
    """

    def __init__(self, email: str, password: str, proxy: str = ""):
        self.email = email
        self.password = password
        self.proxy = proxy or None
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BASE_URL + "/",
        })
        if self.proxy:
            self.s.proxies.update({"http": self.proxy, "https": self.proxy})
        # 会话级状态 (登录后填充)
        self.uin = ""
        self.is_vip = 0
        self.use_downview = 0
        self.quota_now = 0.0       # 剩余额度 (M)
        self.quota_used = 0.0
        self.logged_in = False
        # 额度读写锁 (并发下载时保护 quota_now)
        self.quota_lock = threading.Lock()

    # ----- HTTP helpers -----
    def _get(self, path: str, params=None, headers=None, stream=False, allow_redirects=True):
        url = path if path.startswith("http") else BASE_URL + path
        h = {"X-KM-FROM": KM_FROM + " GET"}
        if headers:
            h.update(headers)
        return self.s.get(url, params=params, headers=h, stream=stream,
                          allow_redirects=allow_redirects, timeout=60)

    def _post(self, path: str, data=None, headers=None):
        url = path if path.startswith("http") else BASE_URL + path
        h = {"X-KM-FROM": KM_FROM + " POST"}
        if headers:
            h.update(headers)
        return self.s.post(url, data=data, headers=h, timeout=60)

    # ----- 登录 -----
    def login(self) -> None:
        # 1) 预热: GET / 与 /login.php 让服务端写 VLIBSID/VOLSESS cookie
        self._get("/")
        self._get("/login.php")

        # 2) 提交登录表单 (form target=iframe_action, 协议层就是普通 POST)
        r = self._post("/login_do.php", data={
            "email": self.email,
            "passwd": self.password,
            "keepalive": "on",
        })
        text = r.text
        m = re.search(r'display_codeinfo\(\s*"(\w+)"', text)
        if not m:
            # 没有出现预期回调,可能是反爬响应
            raise KxxLoginError(f"登录响应异常: {text[:200]!r}")
        code = m.group(1)
        if code != "m100":
            raise KxxLoginError(f"{code} - {LOGIN_ERRORS.get(code, '未知错误')}")
        # 3) 验证登录态: 拉 / 并检查 uin 是否填充
        self._get("/")
        self.logged_in = True

    # ----- 书籍页解析 -----
    def _parse_book_html(self, html: str) -> dict:
        def grab_int(var):
            m = re.search(rf'var\s+{var}\s*=\s*parseInt\(\s*"(-?\d+)"\s*\)', html)
            if m:
                return int(m.group(1))
            m = re.search(rf'var\s+{var}\s*=\s*"(-?\d+)"', html)
            return int(m.group(1)) if m else 0

        def grab_str(var):
            m = re.search(rf'var\s+{var}\s*=\s*"([^"]*)"', html)
            return m.group(1) if m else ""

        m = re.search(r'book_data\.php\?h=([0-9a-zA-Z]+)', html)
        h = m.group(1) if m else ""
        m = re.search(r'<title>([^<]+)</title>', html)
        title = m.group(1).strip() if m else ""
        # 去掉标题里的 " [Kindle漫畫|epub漫畫] [kxx.moe]" 之类尾缀
        title = re.sub(r'\s*\[.*?\]\s*\[kxx\.moe\]\s*$', '', title).strip()

        quota_now = grab_str("quota_now")
        quota_used = grab_str("quota_used")
        return {
            "bookid":       grab_str("bookid"),
            "quota_now":    float(quota_now) if quota_now else 0.0,
            "quota_used":   float(quota_used) if quota_used else 0.0,
            "is_vip":       grab_int("is_vip"),
            "use_downview": grab_int("use_downview"),
            "uin":          grab_str("uin"),
            "book_data_h":  h,
            "title":        title,
        }

    def fetch_book(self, book_code: str) -> dict:
        """GET /c/{code}.htm 并更新会话状态 (bookid / quota 等)."""
        r = self._get(f"/c/{book_code}.htm")
        r.raise_for_status()
        info = self._parse_book_html(r.text)
        info["book_code"] = book_code
        # 更新自身状态 (并发时这里也加锁,避免与下载 worker 同时写 quota)
        with self.quota_lock:
            self.uin          = info["uin"]
            self.is_vip        = info["is_vip"]
            self.use_downview = info["use_downview"]
            self.quota_now    = info["quota_now"]
            self.quota_used   = info["quota_used"]
        return info

    def fetch_volumes(self, h: str) -> list:
        """GET /book_data.php?h={hash} 解析 volinfo= 行."""
        r = self._get(f"/book_data.php?h={h}")
        r.raise_for_status()
        vols = []
        for m in re.finditer(r'volinfo=([^\n\r"]+)', r.text):
            parts = m.group(1).split(",")
            if len(parts) < 12:
                continue
            try:
                v = {
                    "vol_id":   parts[0].strip(),
                    "category": parts[3].strip() if len(parts) > 3 else "",
                    "order":    parts[4].strip() if len(parts) > 4 else "",
                    "name":     parts[5].strip() if len(parts) > 5 else "",
                    "pages":    parts[6].strip() if len(parts) > 6 else "",
                    "zip_mb":   float(parts[9]) if len(parts) > 9 and parts[9] else 0.0,
                    "mobi_mb":  float(parts[10]) if len(parts) > 10 and parts[10] else 0.0,
                    "epub_mb":  float(parts[11]) if len(parts) > 11 and parts[11] else 0.0,
                    "date":     parts[13].strip() if len(parts) > 13 else "",
                }
                vols.append(v)
            except (ValueError, IndexError):
                continue
        return vols

    def get_download_url(self, bookid: str, volid: str, fmt_code: int) -> dict:
        """GET /getdownurl.php?b=&v=&mobi=&vip=0&json=1 -> {url, name}."""
        r = self._get("/getdownurl.php", params={
            "b": bookid, "v": volid, "mobi": fmt_code, "vip": "0", "json": "1",
        })
        text = r.text
        # 先看是否有内联错误码
        m = re.search(r'display_codeinfo\(\s*"(\w+)"', text)
        if m:
            code = m.group(1)
            if code != "m100":  # m100 不会出现在 getdownurl,出现就是错误
                raise RuntimeError(f"获取下载URL被拒: {code} - {DL_ERRORS.get(code, '未知')}")
        # 纯 JSON 解析
        try:
            return r.json()
        except ValueError:
            # 容错: 偶尔返回 <script>parent.down_add({...});</script>
            m = re.search(r'down_add\(\s*(\{.*?\})\s*\)', text, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            # 容错: 直接找 {"url":...} 子串
            m = re.search(r'\{[^{}]*"url"[^{}]*\}', text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            # 403 文本
            if r.status_code == 403 or "403" in text:
                raise RuntimeError(f"获取下载URL失败 (403): {text[:200]!r}")
            raise RuntimeError(f"获取下载URL响应无法解析: {text[:300]!r}")

    def download_file(self, url: str, save_path: Path,
                      expected_min_bytes: int = 1024,
                      progress_cb=None) -> int:
        """流式下载文件到本地, 返回字节数.

        progress_cb(downloaded_bytes, total_bytes) 可选, 用于驱动进度条.
        """
        # 下载 URL 可能在第三方域名, 仍带上 cookie + UA
        with self.s.get(url, stream=True, allow_redirects=True,
                        headers={"X-KM-FROM": KM_FROM + " GET"},
                        timeout=300) as r:
            r.raise_for_status()
            total = 0
            total_bytes = int(r.headers.get("Content-Length", 0)) or 0
            tmp = save_path.with_suffix(save_path.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
                        if progress_cb is not None:
                            try:
                                progress_cb(total, total_bytes)
                            except Exception:
                                pass
            if total < expected_min_bytes:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"下载内容过小 ({total} bytes), 可能是错误页或额度不足"
                )
            tmp.replace(save_path)
        return total

    # ----- 额度操作 (线程安全) -----
    def reserve_quota(self, needed_mb: float) -> bool:
        """尝试为本卷预留额度, 成功返回 True (并扣减本地预估)."""
        with self.quota_lock:
            if self.quota_now >= needed_mb:
                self.quota_now = max(0.0, self.quota_now - needed_mb)
                return True
            return False

    def refresh_quota(self, book_code: str) -> float:
        """从服务端刷新真实额度, 返回当前 quota_now."""
        try:
            self.fetch_book(book_code)
        except Exception:
            pass
        with self.quota_lock:
            return self.quota_now

    def get_quota(self) -> float:
        with self.quota_lock:
            return self.quota_now


def load_accounts(path: str) -> list:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到账号配置 {path}. 请参考 accounts.json 填写."
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    accs = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(accs, list) or not accs:
        raise ValueError(f"{path} 中没有可用的 accounts 列表")
    cleaned = []
    for a in accs:
        email = (a.get("email") or "").strip()
        pwd = a.get("password") or ""
        if not email or not pwd:
            continue
        # 跳过用户没填的占位
        if email.startswith("your_") or pwd.startswith("your_"):
            continue
        cleaned.append({
            "email": email,
            "password": pwd,
            "download_dir": (a.get("download_dir") or "").strip(),
            "proxy": (a.get("proxy") or "").strip(),
        })
    return cleaned


def load_proxies(path: str) -> list:
    """从文件读取代理列表 (每行一个 URL, # 开头为注释)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到代理文件 {path}")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def parse_book_url(url: str) -> str:
    """从 https://kxx.moe/c/12347e.htm 提取书籍短码 12347e."""
    m = re.search(r'/c/([A-Za-z0-9]+)\.htm', url)
    if not m:
        raise ValueError(f"URL 无法识别, 应为 https://kxx.moe/c/<code>.htm : {url}")
    return m.group(1)


def fmt_size_mb(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.2f}G"
    return f"{mb:.1f}M"


def fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n/1024/1024:.1f}M"
    if n >= 1024:
        return f"{n/1024:.1f}K"
    return f"{n}B"


# ---------- 并发下载 ----------

def pick_client_for_quota(clients, needed_mb: float):
    """在 clients 列表中选一个额度足够的 client, 没有则返回额度最高的."""
    best = None
    best_quota = -1.0
    for _, acc, c in clients:
        q = c.get_quota()
        if q >= needed_mb:
            return acc, c
        if q > best_quota:
            best_quota = q
            best = (acc, c)
    return best


def download_one_volume(
    idx: int,
    total: int,
    vol: dict,
    info: dict,
    fmt: str,
    fmt_code: int,
    out_dir: Path,
    clients,                       # list of (client_idx, acc_dict, KxxClient)
    book_code: str,
    retries: int = 3,
):
    """下载单卷 (worker 函数, 可在 ThreadPoolExecutor 中并发执行).

    每卷下载时显示独立 tqdm 子进度条 (实时百分比).
    """
    size_mb = vol[f"{fmt}_mb"]
    needed = max(1.0, size_mb * 1.5)

    # 文件名: 卷名.{fmt} (去除 [Kmoe] 水印)
    seq_name = sanitize_filename(vol["name"], max_len=80) or f"vol{vol['vol_id']}"
    local_name = f"{seq_name}.{fmt}"
    save_path = out_dir / local_name

    # 已存在跳过
    if save_path.exists() and save_path.stat().st_size > 1024:
        log(f"[{idx:>2}/{total}] 已存在跳过: {local_name}")
        return ("skip", vol, local_name, save_path.stat().st_size)

    # 选额度足够的账号 (遍历一次, 失败再轮换)
    last_err = None
    for attempt in range(1, retries + 1):
        acc, c = pick_client_for_quota(clients, needed)
        if c is None:
            last_err = RuntimeError("无可用账号")
            break
        if c.get_quota() < needed:
            # 没有账号额度足够
            last_err = RuntimeError(
                f"全部账号额度不足 (最高 {fmt_size_mb(c.get_quota())} < 需要 {needed:.1f}M)"
            )
            break

        # 预留额度 (本地预估扣减, 避免并发重复扣)
        if not c.reserve_quota(needed):
            time.sleep(0.5)
            continue

        try:
            dl = c.get_download_url(info["bookid"], vol["vol_id"], fmt_code)
            url = dl.get("url")
            if not url:
                raise RuntimeError(f"返回 JSON 无 url 字段: {dl}")
            # 优先用服务端给的文件名 (去掉 [Kmoe] 水印, 不加本地序号)
            remote_name = dl.get("name") or ""
            if remote_name and remote_name.lower().endswith(f".{fmt}"):
                base_remote = remote_name[:-len(f".{fmt}")]
                final_name = f"{sanitize_filename(base_remote)}.{fmt}"
                save_path = out_dir / final_name
                local_name = final_name
            # 再次检查是否已存在 (并发时另一 worker 可能已下完)
            if save_path.exists() and save_path.stat().st_size > 1024:
                log(f"[{idx:>2}/{total}] 已存在跳过: {local_name}")
                return ("skip", vol, local_name, save_path.stat().st_size)

            # 下载 (每卷独立 tqdm 子进度条, 显示实时百分比)
            pbar_box = {"pbar": None}

            def progress_cb(done, total_bytes):
                if not _HAS_TQDM:
                    return
                if pbar_box["pbar"] is None:
                    pbar_box["pbar"] = tqdm(
                        total=total_bytes if total_bytes else None,
                        desc=f"[{idx:>2}/{total}] {local_name[:18]}",
                        unit="B", unit_scale=True, unit_divisor=1024,
                        dynamic_ncols=True, leave=True,
                    )
                else:
                    pb = pbar_box["pbar"]
                    if pb.total is None and total_bytes:
                        pb.total = total_bytes
                    pb.n = done
                    pb.refresh()

            log(f"[{idx:>2}/{total}] 开始下载 {vol['name']} "
                f"({fmt}={size_mb:.1f}M) 账号={acc['email']}")
            try:
                total_bytes = c.download_file(url, save_path, progress_cb=progress_cb)
            finally:
                if pbar_box["pbar"] is not None:
                    pbar_box["pbar"].close()
            log(f"[{idx:>2}/{total}] 完成 {local_name} ({fmt_bytes(total_bytes)})")
            # 服务端刷新真实额度
            real_q = c.refresh_quota(book_code)
            log(f"    [{acc['email']}] 剩余额度: {fmt_size_mb(real_q)}")
            return ("ok", vol, local_name, total_bytes)

        except Exception as e:
            last_err = e
            log(f"[{idx:>2}/{total}] 尝试 {attempt}/{retries} 失败 "
                f"({acc['email']}): {e}")
            # 失败时把预留额度还回去
            with c.quota_lock:
                c.quota_now += needed
            # 清理 .part
            for bad in out_dir.glob("*.part"):
                try:
                    bad.unlink(missing_ok=True)
                except Exception:
                    pass
            if attempt < retries:
                time.sleep(3 * attempt)
            # 下一次重试可能换账号

    return ("fail", vol, local_name, last_err)


def main():
    ap = argparse.ArgumentParser(
        description="kxx.moe 漫画下载器 (多账号, 额度监控, 纯协议, 并发, 代理)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python kxx_downloader.py https://kxx.moe/c/12347e.htm --epub\n"
               "  python kxx_downloader.py https://kxx.moe/c/12347e.htm --mobi\n"
               "  python kxx_downloader.py <url> --epub --workers 4\n"
               "  python kxx_downloader.py <url> --epub --proxy http://127.0.0.1:7890\n"
               "  python kxx_downloader.py <url> --epub --proxy-file proxies.txt\n"
               "  python kxx_downloader.py <url> --list\n",
    )
    ap.add_argument("url", help="书籍页 URL, 如 https://kxx.moe/c/12347e.htm")
    fmt_g = ap.add_mutually_exclusive_group()
    fmt_g.add_argument("--epub", action="store_const", const="epub",
                       dest="fmt", help="下载 epub 格式")
    fmt_g.add_argument("--mobi", action="store_const", const="mobi",
                       dest="fmt", help="下载 mobi 格式 (默认)")
    fmt_g.add_argument("--zip", action="store_const", const="zip",
                       dest="fmt", help="下载 zip 源图 (需 VIP)")
    ap.add_argument("--out", default="", help="下载根目录 (默认 ./downloads)")
    ap.add_argument("--accounts", default="accounts.json",
                    help="账号配置文件路径 (默认 ./accounts.json)")
    ap.add_argument("--list", action="store_true",
                    help="仅列出全部卷信息, 不下载")
    ap.add_argument("--start", type=int, default=1,
                    help="从第 N 卷开始下载 (1-based, 默认 1)")
    ap.add_argument("--end", type=int, default=0,
                    help="到第 N 卷结束 (0=全部, 默认 0)")

    # 代理参数
    ap.add_argument("--proxy", default="",
                    help="HTTP/SOCKS5 代理 (如 http://127.0.0.1:7890 或 "
                         "socks5://127.0.0.1:1080); 全局生效,所有账号共用")
    ap.add_argument("--proxy-file", default="",
                    help="代理列表文件 (每行一个 URL); 按账号索引循环分配, "
                         "比账号数多则取前 N 个")

    # 并发参数
    ap.add_argument("--workers", type=int, default=4,
                    help="并发下载线程数 (默认 4; 设为 1 即顺序下载)")
    ap.add_argument("--no-concurrent", action="store_true",
                    help="禁用并发, 顺序下载 (相当于 --workers 1)")

    args = ap.parse_args()

    # 默认格式 mobi
    fmt = args.fmt or "mobi"
    fmt_code = FMT_CODE[fmt]
    workers = 1 if args.no_concurrent else max(1, args.workers)

    # 1) 解析书籍 URL
    book_code = parse_book_url(args.url)
    log(f"[*] 书籍短码: {book_code}")

    # 2) 加载账号
    accounts = load_accounts(args.accounts)
    if not args.list and not accounts:
        log("[!] 没有可用账号 (请编辑 accounts.json 填入真实 email/password)")
        sys.exit(2)

    # 3) 解析代理: --proxy 优先; 否则 --proxy-file 按账号分配; 否则用 accounts.json 里的 proxy 字段
    proxies_pool = []
    if args.proxy:
        proxies_pool = [args.proxy] * len(accounts)
        log(f"[*] 全局代理: {args.proxy}")
    elif args.proxy_file:
        proxies_pool = load_proxies(args.proxy_file)
        if not proxies_pool:
            log("[x] --proxy-file 指定但文件为空")
            sys.exit(2)
        log(f"[*] 代理池大小: {len(proxies_pool)} (将按账号索引循环分配)")

    # 4) 登录所有账号 (顺序登录, 避免并发登录触发风控)
    clients = []  # list of (client_idx, acc_dict, KxxClient)
    for i, acc in enumerate(accounts):
        # 决定该账号用的代理
        if proxies_pool:
            proxy = proxies_pool[i % len(proxies_pool)]
        else:
            proxy = acc.get("proxy", "")
        c = KxxClient(acc["email"], acc["password"], proxy=proxy)
        try:
            c.login()
            tag = f" via {proxy}" if proxy else ""
            log(f"[+] 账号登录成功: {acc['email']}{tag}")
            clients.append((i, acc, c))
        except Exception as e:
            tag = f" (proxy={proxy})" if proxy else ""
            log(f"[-] 账号登录失败 {acc['email']}{tag}: {e}")

    if not args.list and not clients:
        log("[x] 没有账号可用, 退出")
        sys.exit(3)

    # 5) 拉书籍信息 + 卷目录 (用第一个账号)
    primary_acc, primary_c = (clients[0][1], clients[0][2]) if clients \
        else ({"download_dir": ""}, KxxClient("", ""))
    info = primary_c.fetch_book(book_code)
    log(f"[*] 书名: {info['title']}")
    log(f"[*] bookid={info['bookid']} is_vip={info['is_vip']} "
        f"use_downview={info['use_downview']} uin={info['uin'] or '(匿名)'}")
    if not info["bookid"]:
        log(f"[x] 解析 bookid 失败, 原始页面可能改版. 请检查 URL.")
        sys.exit(4)
    if not info["book_data_h"]:
        log(f"[x] 解析 book_data hash 失败.")
        sys.exit(4)

    volumes = primary_c.fetch_volumes(info["book_data_h"])
    if not volumes:
        log("[x] 未获取到任何卷, 可能 hash 过期, 重试一次...")
        info = primary_c.fetch_book(book_code)
        volumes = primary_c.fetch_volumes(info["book_data_h"])
    log(f"[*] 共 {len(volumes)} 卷")

    # 6) 打印各账号初始额度
    for _, acc, c in clients:
        log(f"[*] 账号 {acc['email']} 初始额度: {fmt_size_mb(c.get_quota())}")

    # 7) --list 模式
    if args.list:
        log(f"\n{'#':>3}  {'vol_id':>8}  {'类别':<6}  {'顺序':>5}  {'名称':<28}  "
            f"{'zip':>7}  {'mobi':>7}  {'epub':>7}  {'日期':<12}")
        log("-" * 110)
        for i, v in enumerate(volumes, 1):
            log(f"{i:>3}  {v['vol_id']:>8}  {v['category']:<6}  {v['order']:>5}  "
                f"{v['name']:<28}  {v['zip_mb']:>6.1f}M  {v['mobi_mb']:>6.1f}M  "
                f"{v['epub_mb']:>6.1f}M  {v['date']:<12}")
        return

    # 8) 准备下载目录
    base_dir = args.out or primary_acc.get("download_dir") or "downloads"
    book_dir_name = sanitize_filename(info["title"] or book_code, max_len=60)
    out_dir = Path(base_dir) / book_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"[*] 下载目录: {out_dir.resolve()}")

    # 选定范围
    start_n = max(1, args.start)
    end_n = len(volumes) if args.end <= 0 else min(args.end, len(volumes))
    todo = volumes[start_n - 1:end_n]
    log(f"[*] 计划下载 {len(todo)} 卷 ({start_n}..{end_n}) 格式={fmt} "
        f"并发={workers}")

    # 9) 下载 (每卷独立显示 tqdm 实时百分比进度条)
    if not _HAS_TQDM:
        log("[i] 未安装 tqdm, 进度只显示文本. 安装: pip install tqdm")

    results = []  # list of (status, vol, local_name, bytes_or_err)
    if workers == 1:
        # 顺序下载 (进度条干净显示)
        for i, vol in enumerate(todo, start=start_n):
            r = download_one_volume(i, len(volumes), vol, info, fmt, fmt_code,
                                    out_dir, clients, book_code)
            results.append(r)
    else:
        # 并发下载 (每卷独立进度条, 多条会上下跳动; 如需干净显示用 --workers 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for i, vol in enumerate(todo, start=start_n):
                fu = ex.submit(download_one_volume, i, len(volumes), vol, info,
                               fmt, fmt_code, out_dir, clients, book_code)
                futures[fu] = (i, vol)
            for fu in as_completed(futures):
                try:
                    r = fu.result()
                except Exception as e:
                    i, vol = futures[fu]
                    r = ("fail", vol, "", e)
                results.append(r)

    # 10) 汇总
    success = sum(1 for r in results if r[0] == "ok")
    skipped = sum(1 for r in results if r[0] == "skip")
    failed  = [r for r in results if r[0] == "fail"]
    log("\n" + "=" * 60)
    log(f"下载完成: 成功 {success} 卷, 跳过 {skipped} 卷, 失败 {len(failed)} 卷")
    if failed:
        log("失败卷:")
        for st, vol, name, err in failed:
            log(f"  - {vol['vol_id']} {vol['name']}: {err}")
    # 最终额度
    log("各账号最终额度:")
    for _, acc, c in clients:
        try:
            q = c.refresh_quota(book_code)
        except Exception:
            q = c.get_quota()
        log(f"  {acc['email']}: {fmt_size_mb(q)}")
    log(f"输出目录: {out_dir.resolve()}")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[!] 用户中断")
        sys.exit(130)
    except (ValueError, FileNotFoundError) as e:
        log(f"[x] {e}")
        sys.exit(2)
    except Exception as e:
        log(f"[x] 未捕获错误: {e}")
        sys.exit(1)
