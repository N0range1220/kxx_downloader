# kxx.moe 漫画下载器

支持多账号、并发、代理。

## 安装

```bash
pip install requests tqdm
# SOCKS5 代理额外装:
pip install requests[socks]
```

## 配置账号

编辑 [accounts.json](accounts.json),填入真实账号:

```json
{
  "accounts": [
    {"email": "your@email.com", "password": "your_password"}
  ]
}
```

支持多账号,额度耗尽时自动切换。

## 使用

```bash
# 下载 epub
python kxx_downloader.py https://kxx.moe/c/12347e.htm --epub

# 下载 mobi (默认)
python kxx_downloader.py https://kxx.moe/c/12347e.htm --mobi

# 查看卷目录
python kxx_downloader.py https://kxx.moe/c/12347e.htm --list

# 指定输出目录
python kxx_downloader.py <url> --epub --out /path/to/books

# 范围下载 (第 4-6 卷)
python kxx_downloader.py <url> --epub --start 4 --end 6

# 代理
python kxx_downloader.py <url> --epub --proxy http://127.0.0.1:7890
python kxx_downloader.py <url> --epub --proxy socks5://127.0.0.1:1080
python kxx_downloader.py <url> --epub --proxy-file proxies.txt

# 并发控制
python kxx_downloader.py <url> --epub --workers 8      # 8 线程 (默认 4)
python kxx_downloader.py <url> --epub --no-concurrent   # 顺序下载
```

## 参数

| 参数 | 说明 |
|---|---|
| `url` | 书籍页 URL,如 `https://kxx.moe/c/12347e.htm` |
| `--epub` / `--mobi` / `--zip` | 下载格式(默认 mobi,zip 需 VIP) |
| `--out` | 下载目录(默认 `./downloads`) |
| `--accounts` | 账号配置文件(默认 `accounts.json`) |
| `--list` | 只列卷目录,不下载 |
| `--start` / `--end` | 下载卷范围(1-based,`--end 0` 为全部) |
| `--proxy` | 单个代理(HTTP/SOCKS5) |
| `--proxy-file` | 代理池文件(每行一个,按账号循环分配) |
| `--workers` | 并发线程数(默认 4) |
| `--no-concurrent` | 顺序下载 |

## 说明

- **文件名**:自动去除 `[Kmoe]` 水印,不加序号,如 `卷 01.mobi`
- **进度条**:每卷独立 tqdm 实时百分比
- **断点续传**:已下载文件自动跳过,中断后重跑即可
- **额度**:每卷约消耗 `文件大小 × 1.5` M,自动多账号切换

## 常见问题

- **e400 账号密码错误**:检查 `accounts.json`
- **e404 锁定**:登录失败过多,等 24 小时
- **403**:未登录或会话过期
- **e403 额度不足**:加多账号或开 VIP
- **并发报 SSL 错误**:降低 `--workers` 或用 `--no-concurrent`
