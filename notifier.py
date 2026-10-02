"""Opt-in message transports; credentials live in the ignored .env file."""
import os
from urllib.parse import urlparse

import requests


CHANNEL_LABELS = {
    "wecom": "企业微信群机器人",
    "serverchan": "个人微信（Server酱）",
    "qq_onebot": "QQ（本机 OneBot 网关）",
}


def active_channels():
    if os.environ.get("NOTIFY_ENABLED") != "1":
        return []
    chosen = {part.strip() for part in os.environ.get("NOTIFY_CHANNELS", "").split(",")}
    ready = []
    if "wecom" in chosen and os.environ.get("WECOM_WEBHOOK_URL"):
        ready.append("wecom")
    if "serverchan" in chosen and os.environ.get("SERVERCHAN_SENDKEY"):
        ready.append("serverchan")
    if ("qq_onebot" in chosen and os.environ.get("QQ_ONEBOT_TOKEN")
            and (os.environ.get("QQ_GROUP_ID") or os.environ.get("QQ_USER_ID"))):
        ready.append("qq_onebot")
    return ready


def _post(url, **kwargs):
    try:
        response = requests.post(url, timeout=(3, 8), allow_redirects=False, **kwargs)
        if response.is_redirect or response.status_code >= 400:
            raise RuntimeError(f"通道返回 HTTP {response.status_code}")
        return response.json()
    except requests.RequestException as exc:
        # A request exception may contain a credential-bearing URL. Never persist it.
        raise RuntimeError("通道连接失败；检查网络、凭据和本机网关") from exc
    except ValueError as exc:
        raise RuntimeError("通道响应不是 JSON") from exc


def deliver(channel, title, body):
    message = f"{title}\n{body}"[:1800]
    if channel == "wecom":
        url = os.environ["WECOM_WEBHOOK_URL"]
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com"
                or parsed.path != "/cgi-bin/webhook/send" or not parsed.query.startswith("key=")):
            raise RuntimeError("企业微信 Webhook 地址格式不正确")
        result = _post(url, json={"msgtype": "text", "text": {"content": message}})
        if result.get("errcode") != 0:
            raise RuntimeError(f"企业微信拒绝消息，错误码 {result.get('errcode')}")
    elif channel == "serverchan":
        key = os.environ["SERVERCHAN_SENDKEY"]
        if not key.startswith("SCT") or not key.isalnum():
            raise RuntimeError("Server酱 Turbo SendKey 格式不正确")
        result = _post(f"https://sctapi.ftqq.com/{key}.send",
                       data={"title": title[:100], "desp": body[:4000]})
        if result.get("code") != 0:
            raise RuntimeError(f"Server酱拒绝消息，错误码 {result.get('code')}")
    elif channel == "qq_onebot":
        base = os.environ.get("QQ_ONEBOT_BASE", "http://127.0.0.1:3000").rstrip("/")
        parsed = urlparse(base)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or not parsed.port or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("QQ 网关只能使用本机 127.0.0.1 HTTP 地址")
        group = os.environ.get("QQ_GROUP_ID", "").strip()
        user = os.environ.get("QQ_USER_ID", "").strip()
        target = group or user
        if not target.isdigit():
            raise RuntimeError("QQ 接收目标须为数字 ID")
        action = "send_group_msg" if group else "send_private_msg"
        field = "group_id" if group else "user_id"
        result = _post(f"{base}/{action}",
                       headers={"Authorization": "Bearer " + os.environ["QQ_ONEBOT_TOKEN"]},
                       json={field: int(target), "message": message, "auto_escape": True})
        if result.get("status") != "ok" or result.get("retcode") != 0:
            raise RuntimeError(f"QQ 网关拒绝消息，错误码 {result.get('retcode')}")
    else:
        raise RuntimeError("未知消息通道")
