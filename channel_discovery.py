"""Discover, validate, and promote public channel adapters from GitHub catalogs.

GitHub is used as a versioned adapter catalog, never as proof that a route works.
Every discovered route remains a candidate until the local reader extracts real
items from it.  No third-party automation project is executed by this module.
"""
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
from urllib.parse import urlparse

from db import connect
from scanner import _rss_rows


PROVIDERS = {
    "rsshub": {
        "repo": "DIYgod/RSSHub",
        "route_root": "lib/routes/",
        "queries": ("优惠", "折扣", "特惠", "低价", "免费", "试用", "新品", "上新",
                    "discount", "sale", "offer", "price", "free", "crowdfunding", "freegames"),
    },
}
RELEVANT = re.compile(
    r"优惠|折扣|促销|特惠|低价|降价|免费|试用|众筹|新品|上新|"
    r"discount|sale|offer|price|free|trial|crowdfunding|new\s+products?",
    re.I,
)


def _gh(*args, timeout=45):
    try:
        result = subprocess.run(["gh", *args], check=True, capture_output=True,
                                text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("未安装 GitHub CLI（gh）") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub CLI 查询超时") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "GitHub CLI 查询失败").strip()
        raise RuntimeError(message[:400]) from exc
    return result.stdout


def _field(source, name):
    match = re.search(rf"\b{name}\s*:\s*(['\"])(.*?)\1", source, re.S)
    return match.group(2).strip() if match else ""


def _flag(source, name):
    match = re.search(rf"\b{name}\s*:\s*(true|false)", source)
    return bool(match and match.group(1) == "true")


def parse_rsshub_route(path, source, base="http://127.0.0.1:1200"):
    """Return a normalized public feed candidate or None for unrelated routes."""
    category_match = re.search(r"\bcategories\s*:\s*\[([^]]*)\]", source, re.S)
    categories = set(re.findall(r"['\"]([\w-]+)['\"]", category_match.group(1))) if category_match else set()
    name = _field(source, "name")
    example = _field(source, "example")
    path_hint = str(PurePosixPath(path).with_suffix(""))
    if not example or not example.startswith("/") or ":" in example:
        return None
    if not ({"shopping", "game"} & categories):
        return None
    if not RELEVANT.search(" ".join((path_hint, name, source[:5000]))):
        return None
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ValueError("RSSHUB_BASE 必须是带端口的本机 127.0.0.1 HTTP 地址")
    namespace = example.strip("/").split("/", 1)[0]
    requires_auth = _flag(source, "requireConfig")
    requires_browser = _flag(source, "requirePuppeteer") or _flag(source, "antiCrawler")
    lowered = " ".join((path_hint, name)).lower()
    category = "新品与补货" if re.search(r"crowdfunding|newproducts|new-products|上新|新品|众筹", lowered) else "零售优惠"
    return {
        "platform": namespace.upper(),
        "name": name or example,
        "category": category,
        "url": base.rstrip("/") + example,
        "method": "rsshub",
        "parser": "",
        "route_example": example,
        "requires_auth": int(requires_auth),
        "requires_browser": int(requires_browser),
        "state": "rejected" if requires_auth or requires_browser else "discovered",
        "reason": ("路由需要私有配置或账号" if requires_auth else
                   "路由声明需要浏览器或存在反爬" if requires_browser else
                   "等待本机真实提取校验"),
    }


def _search_paths(repo, branch, queries, route_root):
    """Read one Git tree instead of exhausting GitHub's code-search quota."""
    payload = json.loads(_gh("api", f"repos/{repo}/git/trees/{branch}?recursive=1"))
    path_terms = {term.lower() for term in queries if term.isascii() and len(term) >= 4}
    path_terms.update(("discount", "sale", "offer", "price", "free", "trial",
                       "crowdfunding", "newproduct", "low-price", "low_price"))
    paths = set()
    for row in payload.get("tree", []):
        path = row.get("path", "")
        lowered = path.lower()
        if (path.startswith(route_root) and path.endswith((".ts", ".tsx"))
                and "/templates/" not in path and "/utils/" not in path
                and any(term in lowered for term in path_terms)):
            paths.add(path)
    # Previously discovered index-style routes remain refreshable even when the
    # useful word appears only inside the source file rather than its filename.
    with connect() as db:
        paths.update(row[0] for row in db.execute(
            "SELECT discovery_path FROM source_candidates WHERE discovery_repo=?", (repo,)))
    return sorted(paths)


def refresh(provider="rsshub", queries=None):
    """Refresh candidates from the current GitHub catalog using gh CLI."""
    config = PROVIDERS.get(provider)
    if not config:
        raise ValueError("不支持的 GitHub 渠道目录")
    repo = config["repo"]
    metadata = json.loads(_gh("api", f"repos/{repo}"))
    branch = metadata.get("default_branch") or "master"
    paths = _search_paths(repo, branch, queries or config["queries"], config["route_root"])
    discovered = rejected = fetch_errors = 0
    for path in paths:
        encoded = path.replace("/", "%2F")
        try:
            source = _gh("api", f"repos/{repo}/contents/{encoded}?ref={branch}",
                         "-H", "Accept: application/vnd.github.raw+json")
        except RuntimeError:
            fetch_errors += 1
            continue
        candidate = parse_rsshub_route(path, source, os.environ.get("RSSHUB_BASE", "http://127.0.0.1:1200"))
        if not candidate:
            continue
        candidate.update(
            discovery_provider=provider,
            discovery_repo=repo,
            discovery_path=path,
            discovery_url=f"https://github.com/{repo}/blob/{branch}/{path}",
            repo_license=(metadata.get("license") or {}).get("spdx_id") or "",
            repo_pushed_at=metadata.get("pushed_at"),
        )
        keys = tuple(candidate)
        with connect() as db:
            existing = db.execute("""SELECT state,source_id FROM source_candidates
                WHERE discovery_repo=? AND discovery_path=? AND route_example=?""",
                (repo, path, candidate["route_example"])).fetchone()
            if existing and existing["state"] in ("validated", "promoted"):
                candidate["state"] = existing["state"]
                candidate["source_id"] = existing["source_id"]
                keys = tuple(candidate)
            db.execute(f"""INSERT INTO source_candidates ({','.join(keys)})
                VALUES ({','.join('?' for _ in keys)})
                ON CONFLICT(discovery_repo,discovery_path,route_example) DO UPDATE SET
                {','.join(f'{key}=excluded.{key}' for key in keys if key not in ('discovery_repo','discovery_path','route_example'))},
                updated_at=CURRENT_TIMESTAMP""", tuple(candidate[key] for key in keys))
        discovered += 1
        rejected += candidate["state"] == "rejected"
    return {"matched_files": len(paths), "discovered": discovered, "rejected": rejected,
            "fetch_errors": fetch_errors,
            "repo": repo, "pushed_at": metadata.get("pushed_at")}


def validate(candidate_id):
    with connect() as db:
        row = db.execute("SELECT * FROM source_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("候选渠道不存在")
    if row["requires_auth"] or row["requires_browser"]:
        raise ValueError("该候选需要账号、私有配置或浏览器验证，不能自动启用")
    try:
        records = _rss_rows(row["url"], os.environ.get("RSSHUB_BASE", "http://127.0.0.1:1200"))
        if not records:
            raise RuntimeError("订阅可访问，但没有解析出公开条目")
    except Exception as exc:
        with connect() as db:
            db.execute("""UPDATE source_candidates SET state='failed',reason='真实提取失败',
                observed_items=0,last_checked=CURRENT_TIMESTAMP,last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?""", (str(exc)[:400], candidate_id))
        return {"id": candidate_id, "state": "failed", "items": 0, "error": str(exc)[:400]}
    with connect() as db:
        db.execute("""UPDATE source_candidates SET state='validated',reason='本机真实提取通过',
            observed_items=?,last_checked=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE id=?""", (len(records), candidate_id))
    return {"id": candidate_id, "state": "validated", "items": len(records)}


def validate_many(ids=None):
    with connect() as db:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            selected = [row[0] for row in db.execute(
                f"SELECT id FROM source_candidates WHERE id IN ({placeholders}) ORDER BY id", ids)]
        else:
            selected = [row[0] for row in db.execute(
                "SELECT id FROM source_candidates WHERE state IN ('discovered','failed') ORDER BY id")]
    return [validate(candidate_id) for candidate_id in selected]


def promote(candidate_id):
    with connect() as db:
        row = db.execute("SELECT * FROM source_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise ValueError("候选渠道不存在")
        if row["state"] not in ("validated", "promoted"):
            raise ValueError("候选渠道必须先通过当前真实提取校验")
        source = db.execute("SELECT id FROM sources WHERE url=? AND method=?",
                            (row["url"], row["method"])).fetchone()
        if not source:
            db.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,'pending',1,60)""",
                (row["platform"], row["name"], row["category"], row["url"], row["method"], row["parser"]))
            source = db.execute("SELECT id FROM sources WHERE url=? AND method=?",
                                (row["url"], row["method"])).fetchone()
        if not source:
            raise RuntimeError("候选渠道接入失败")
        db.execute("""UPDATE source_candidates SET state='promoted',reason='已接入渠道列表',
            source_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (source[0], candidate_id))
        return source[0]
