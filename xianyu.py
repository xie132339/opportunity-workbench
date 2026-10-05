"""Small server-side bridge to the loopback Xianyu collector."""
import json
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests


ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
RESULT_FILE = re.compile(r"^[^/\\]+_full_data\.jsonl$")
PUBLISH_WINDOWS = {"", "最新", "1天内", "3天内", "7天内", "14天内"}


class XianyuError(ValueError):
    pass


def base_url():
    base = os.environ.get("GOOFISH_BASE", "http://127.0.0.1:8000").rstrip("/")
    if base != "http://127.0.0.1:8000":
        raise XianyuError("闲鱼采集服务只能使用本机 127.0.0.1:8000")
    return base


def api(method, path, payload=None):
    if not path.startswith("/") or ".." in path or "//" in path:
        raise XianyuError("闲鱼接口路径无效")
    try:
        response = requests.request(method, base_url() + path, json=payload,
                                    timeout=(3, 12), allow_redirects=False)
    except requests.RequestException as exc:
        raise XianyuError("闲鱼采集服务未运行或暂不可达") from exc
    if response.is_redirect:
        raise XianyuError("闲鱼采集服务发生意外跳转")
    if not response.ok:
        # The upstream detail can contain a task name or credentials; never echo it.
        raise XianyuError(f"闲鱼采集服务拒绝操作（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError as exc:
        raise XianyuError("闲鱼采集服务返回格式错误") from exc


def snapshot():
    health = api("GET", "/health")
    if health.get("status") != "healthy":
        raise XianyuError("闲鱼采集服务尚未就绪")
    accounts = api("GET", "/api/accounts")
    tasks = api("GET", "/api/tasks")
    files = api("GET", "/api/results/files").get("files", [])
    if not isinstance(accounts, list) or not isinstance(tasks, list) or not isinstance(files, list):
        raise XianyuError("闲鱼采集服务列表格式错误")
    return accounts, tasks, files


def import_account(name, content):
    if not ACCOUNT_NAME.fullmatch(name):
        raise XianyuError("账号别名只能使用 1–50 位字母、数字、下划线或短横线")
    if not content or len(content.encode("utf-8")) > 500_000:
        raise XianyuError("登录态 JSON 不能为空或超过 500 KB")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise XianyuError("登录态不是有效 JSON") from exc
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list) or not any(
        isinstance(cookie, dict)
        and ((domain := str(cookie.get("domain", "")).lstrip(".")) == "goofish.com"
             or domain.endswith(".goofish.com"))
        for cookie in cookies
    ):
        raise XianyuError("登录态缺少闲鱼域名的 Cookie")
    accounts = api("GET", "/api/accounts")
    existing = {entry.get("name") for entry in accounts if isinstance(entry, dict)}
    if name in existing:
        api("PUT", "/api/accounts/" + name, {"content": content})
    else:
        api("POST", "/api/accounts", {"name": name, "content": content})


def price_text(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise XianyuError("价格格式错误") from exc
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise XianyuError("价格须为非负数，最多两位小数")
    return value


def search_options(form, keyword):
    minimum = price_text(form.get("min_price"))
    maximum = price_text(form.get("max_price"))
    if minimum and maximum and Decimal(minimum) > Decimal(maximum):
        raise XianyuError("最低价不能高于最高价")
    try:
        max_pages = int(form.get("max_pages", "3"))
    except ValueError as exc:
        raise XianyuError("抓取页数无效") from exc
    if not 1 <= max_pages <= 10:
        raise XianyuError("抓取页数须在 1–10 之间")
    publish_window = form.get("new_publish_option", "")
    if publish_window not in PUBLISH_WINDOWS:
        raise XianyuError("发布时间范围无效")
    region = form.get("region", "").strip()
    if len(region) > 80:
        raise XianyuError("地区最多 80 字")
    rules = [line.strip() for line in form.get("keyword_rules", "").splitlines() if line.strip()]
    if len(rules) > 20 or any(len(rule) > 80 for rule in rules):
        raise XianyuError("筛选词最多 20 行，每行最多 80 字")
    return {
        "keyword_rules": rules or [keyword],
        "analyze_images": False, "max_pages": max_pages,
        "personal_only": form.get("personal_only") == "1",
        "free_shipping": form.get("free_shipping") == "1",
        "min_price": minimum, "max_price": maximum,
        "new_publish_option": publish_window or None,
        "region": region or None,
    }


def create_task(form):
    name = form.get("task_name", "").strip()
    keyword = form.get("keyword", "").strip()
    account_name = form.get("account", "").strip()
    if not name or not keyword or len(name) > 80 or len(keyword) > 80:
        raise XianyuError("任务名称和关键词必填，最多 80 字")
    accounts = api("GET", "/api/accounts")
    account = next((entry for entry in accounts if entry.get("name") == account_name), None)
    if not account:
        raise XianyuError("请先在本页导入本人闲鱼登录态")
    payload = {
        "task_name": name, "keyword": keyword, "enabled": True,
        "decision_mode": "keyword",
        "account_strategy": "fixed", "account_state_file": account["path"],
        **search_options(form, keyword),
    }
    api("POST", "/api/tasks/", payload)


def update_task(task_id, task, form):
    if task.get("decision_mode") != "keyword":
        raise XianyuError("此页面只编辑关键词模式任务")
    payload = {**search_options(form, task.get("keyword", "")),
               "enabled": form.get("enabled") == "1"}
    api("PATCH", f"/api/tasks/{task_id}", payload)


def result_url(filename):
    if not RESULT_FILE.fullmatch(filename) or ".." in filename:
        raise XianyuError("闲鱼结果文件名无效")
    return base_url() + "/api/results/" + quote(filename, safe="")
