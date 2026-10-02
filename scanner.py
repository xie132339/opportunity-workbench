"""Public-page adapters and changedetection API consumption.

An HTTP 200 alone is never reported as a working adapter. An adapter is healthy
only after it extracts at least one candidate with a usable title and URL.
"""
import hashlib
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import feedparser
import requests

from db import connect

USER_AGENT = "OpportunityWorkbench/0.1 (personal low-frequency public-source research)"


def _public_url(url):
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or "." not in host:
        raise ValueError("Only public HTTPS URLs are supported")
    if host.endswith((".local", ".internal")) or host in ("localhost",):
        raise ValueError("Local addresses cannot be scanned")
    if re.fullmatch(r"[0-9.]+", host):
        raise ValueError("IP-address URLs cannot be scanned")


def _local_adapter_url(url, base, prefix):
    """Only a configured loopback service may supply adapter data."""
    target, service = urlparse(url), urlparse(base)
    if (service.scheme != "http" or service.hostname != "127.0.0.1"
            or not service.port or service.username or service.password
            or target.scheme != "http" or target.hostname != "127.0.0.1"
            or target.port != service.port or target.username or target.password
            or target.fragment or not target.path.startswith(prefix)
            or ".." in target.path):
        raise ValueError("适配器地址须是配置的本机服务和有效路径")


def _fetch(url):
    _public_url(url)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(5, 18),
                            allow_redirects=False, stream=True)
    if response.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError("入口发生跳转，需人工核对新地址")
    if response.status_code in (401, 403, 429):
        raise PermissionError(f"HTTP {response.status_code}: 可能需登录、限流或验证")
    response.raise_for_status()
    kind = response.headers.get("content-type", "")
    if not any(x in kind for x in ("text/html", "application/xhtml+xml")):
        raise RuntimeError(f"非 HTML 内容: {kind[:80]}")
    data = b""
    for chunk in response.iter_content(65536):
        data += chunk
        if len(data) > 3_000_000:
            break
    response._content = data
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _allowed(parser, url):
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path
    if parser == "smzdm":
        return host == "www.smzdm.com" and re.match(r"^/p/\d+/?$", path)
    if parser == "apple":
        return host == "www.apple.com.cn" and path.startswith("/shop/product/")
    if parser == "mi":
        return host == "www.mi.com" and path == "/shop/buy"
    if parser == "ccgp":
        return host == "www.ccgp.gov.cn" and "/cggg/" in path and "/gkzb/" in path and path.endswith(".htm")
    if parser == "yiwugo":
        return host == "www.yiwugo.com" and "/product/" in path
    if parser == "kongfz":
        return host == "book.kongfz.com" and re.fullmatch(r"/\d+/\d+/?", path)
    if parser == "suning":
        return host == "product.suning.com" and re.fullmatch(r"/\d+/\d+\.html", path)
    if parser == "lenovo":
        return host == "item.lenovo.com.cn" and re.fullmatch(r"/product/\d+\.html", path)
    if parser == "honor":
        return host == "www.honor.com" and re.fullmatch(r"/cn/shop/product/\d+\.html", path)
    return False


def extract_html(parser, page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        title = " ".join(link.get_text(" ", strip=True).split())
        href = urljoin(page_url, link.get("href", ""))
        if not 6 <= len(title) <= 300 or not _allowed(parser, href):
            continue
        canonical = href.split("?", 1)[0] if parser != "mi" else href.split("&", 1)[0]
        if canonical not in found or len(title) > len(found[canonical][0]):
            found[canonical] = (title, canonical, "")
        if len(found) >= 80:
            break
    return list(found.values())


def _monitor_rows(source):
    base = os.environ.get("CHANGED_API_BASE", "http://127.0.0.1:5001").rstrip("/")
    if base != "http://127.0.0.1:5001":
        raise RuntimeError("当前仅允许本机监控 API")
    token = os.environ.get("CHANGED_API_TOKEN")
    if not token or not source["watch_uuid"]:
        raise RuntimeError("监控 UUID 或 API 凭据未配置")
    url = f"{base}/api/v1/watch/{source['watch_uuid']}"
    headers = {"x-api-key": token}
    watch = requests.get(url, headers=headers, timeout=12)
    watch.raise_for_status()
    info = watch.json()
    if info.get("last_error"):
        raise RuntimeError("监控底座最近一次检查失败")
    history = requests.get(url + "/history", headers=headers, timeout=12)
    history.raise_for_status()
    result = []
    with connect() as db:
        known = {row[0] for row in db.execute("SELECT external_key FROM events WHERE source_id=?", (source["id"],))}
    unseen = [stamp for stamp in sorted(history.json().keys())
              if f"{source['watch_uuid']}:{stamp}" not in known]
    for stamp in unseen[:100]:
        snap = requests.get(url + "/history/" + stamp, headers=headers, timeout=12)
        snap.raise_for_status()
        body = snap.text[:1500]
        result.append((f"{source['watch_uuid']}:{stamp}",
                       source["name"] + " · 页面变化", source["url"], body))
    return result


def _rss_rows(url, local_base=None):
    if local_base:
        _local_adapter_url(url, local_base, "/")
    else:
        _public_url(url)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(5, 18),
                            allow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError("订阅地址发生跳转，需核对新地址")
    if response.status_code in (401, 403, 429):
        raise PermissionError(f"HTTP {response.status_code}: 可能需登录或限流")
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise RuntimeError("订阅内容超过 2 MB 限制")
    feed = feedparser.parse(response.content)
    rows = []
    for entry in feed.entries[:80]:
        link = entry.get("link", "")
        title = " ".join(entry.get("title", "").split())
        try:
            _public_url(link)
        except ValueError:
            continue
        if not title:
            continue
        snippet = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)[:1500]
        rows.append((entry.get("id") or link, title[:300], link, snippet))
    return rows


def _goofish_rows(url):
    base = os.environ.get("GOOFISH_BASE", "http://127.0.0.1:8000").rstrip("/")
    _local_adapter_url(url, base, "/api/results/")
    if not urlparse(url).path.endswith(".jsonl"):
        raise ValueError("闲鱼结果地址须以 .jsonl 结尾")
    auth = None
    if os.environ.get("GOOFISH_API_USER") and os.environ.get("GOOFISH_API_PASSWORD"):
        auth = (os.environ["GOOFISH_API_USER"], os.environ["GOOFISH_API_PASSWORD"])
    response = requests.get(url, params={"page": 1, "limit": 100}, auth=auth,
                            timeout=(3, 12), allow_redirects=False)
    if response.status_code in (401, 403):
        raise PermissionError("闲鱼结果接口需登录")
    if response.is_redirect:
        raise RuntimeError("闲鱼结果接口发生跳转，需核对登录状态")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError("闲鱼结果接口格式不符合预期")
    rows = []
    for record in data["items"]:
        if not isinstance(record, dict):
            continue
        item = record.get("商品信息") or {}
        if not isinstance(item, dict):
            continue
        link = str(item.get("商品链接") or "")
        title = " ".join(str(item.get("商品标题") or "").split())[:300]
        try:
            _public_url(link)
        except ValueError:
            continue
        if not title:
            continue
        ask = str(item.get("当前售价") or "未知")
        hint = "AI 推荐" if (record.get("ai_analysis") or {}).get("is_recommended") else "未标记 AI 推荐"
        rows.append((str(item.get("商品ID") or link), title, link,
                     f"闲鱼挂牌价 {ask}；{hint}。挂牌价并非成交价或可转卖利润。"))
    return rows


def _passes_strategy(db, title, category):
    rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
    if not rules:
        return True
    for rule in rules:
        if rule["category"] and rule["category"] != category:
            continue
        include = [x.strip().lower() for x in rule["include_words"].split(",") if x.strip()]
        exclude = [x.strip().lower() for x in rule["exclude_words"].split(",") if x.strip()]
        low = title.lower()
        if include and not any(x in low for x in include):
            continue
        if any(x in low for x in exclude):
            continue
        return True
    return False


def scan_source(source_id):
    with connect() as db:
        source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not source:
            raise ValueError("来源不存在")
        if not source["enabled"]:
            raise ValueError("来源已暂停或仍是候选")
        prior = db.execute("SELECT COUNT(*) FROM events WHERE source_id=?", (source_id,)).fetchone()[0]
        db.execute("UPDATE sources SET last_checked=CURRENT_TIMESTAMP WHERE id=?", (source_id,))

    try:
        if source["method"] == "monitor":
            records = _monitor_rows(source)
        elif source["method"] == "rss":
            records = _rss_rows(source["url"])
        elif source["method"] == "rsshub":
            records = _rss_rows(source["url"], os.environ.get("RSSHUB_BASE", "http://127.0.0.1:1200").rstrip("/"))
        elif source["method"] == "goofish":
            records = _goofish_rows(source["url"])
        elif source["method"] == "html":
            html = _fetch(source["url"])
            records = [(url, title, url, snippet)
                       for title, url, snippet in extract_html(source["parser"], source["url"], html)]
        else:
            raise RuntimeError("此来源仅支持人工录入")
        if not records and source["method"] == "monitor" and prior:
            with connect() as db:
                db.execute("""UPDATE sources SET status='healthy',last_success=CURRENT_TIMESTAMP,
                    last_error=NULL WHERE id=?""", (source_id,))
            return {"source_id": source_id, "status": "healthy", "new": 0,
                    "baseline": False, "seen": 0}
        if not records:
            raise RuntimeError("页面可访问，但当前适配器未提取到符合条件的条目")
    except Exception as exc:
        status = "login_required" if isinstance(exc, PermissionError) else "failed"
        with connect() as db:
            db.execute("UPDATE sources SET status=?,last_error=? WHERE id=?",
                       (status, str(exc)[:400], source_id))
        return {"source_id": source_id, "status": status, "new": 0, "error": str(exc)[:400]}

    added = 0
    with connect() as db:
        for key, title, url, snippet in records:
            digest = hashlib.sha256((title + "\n" + snippet).encode()).hexdigest()
            result = db.execute("""INSERT OR IGNORE INTO events
                (source_id,external_key,title,url,snippet,fingerprint,is_baseline)
                VALUES(?,?,?,?,?,?,?)""",
                (source_id,key,title,url,snippet,digest,int(prior == 0)))
            if not result.rowcount:
                continue
            added += 1
            opp = db.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,url) VALUES(?,?,?,?,?)""",
                (result.lastrowid, source_id, title, source["category"], url))
            if prior and _passes_strategy(db, title, source["category"]):
                db.execute("""INSERT OR IGNORE INTO notifications
                    (event_id,opportunity_id) VALUES(?,?)""",
                    (result.lastrowid, opp.lastrowid))
        db.execute("""UPDATE sources SET status='healthy',last_success=CURRENT_TIMESTAMP,
            last_error=NULL WHERE id=?""", (source_id,))
    return {"source_id": source_id, "status": "healthy", "new": added,
            "baseline": prior == 0, "seen": len(records)}


def scan_all(due_only=False):
    with connect() as db:
        sql = "SELECT id FROM sources WHERE enabled=1 AND method!='manual'"
        if due_only:
            sql += " AND (last_checked IS NULL OR datetime(last_checked, '+' || interval_minutes || ' minutes') <= CURRENT_TIMESTAMP)"
        ids = [r[0] for r in db.execute(sql)]
    return [scan_source(source_id) for source_id in ids]
