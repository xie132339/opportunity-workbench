"""Opt-in message transports; credentials live in the ignored .env file."""
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

import requests


CHANNEL_LABELS = {
    "wecom": "企业微信群机器人",
    "serverchan": "个人微信（Server酱）",
    "qq_onebot": "QQ（本机 OneBot 网关）",
}
ROOT = Path(__file__).resolve().parent
MESSAGE_KEYS = ("NOTIFY_ENABLED", "NOTIFY_CHANNELS", "WECOM_WEBHOOK_URL",
                "SERVERCHAN_SENDKEY", "QQ_ONEBOT_BASE", "QQ_ONEBOT_TOKEN",
                "QQ_GROUP_ID", "QQ_USER_ID")


def notification_settings():
    """Read the ignored local file each time so the worker sees UI changes."""
    values = {key: os.environ.get(key, "") for key in MESSAGE_KEYS}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip() in values:
                    values[key.strip()] = value.strip()
    values["QQ_ONEBOT_BASE"] = values["QQ_ONEBOT_BASE"] or "http://127.0.0.1:3000"
    return values


def channel_ready(channel, values):
    if channel == "wecom":
        parsed = urlparse(values["WECOM_WEBHOOK_URL"])
        return (parsed.scheme == "https" and parsed.hostname == "qyapi.weixin.qq.com"
                and parsed.path == "/cgi-bin/webhook/send"
                and parsed.query.startswith("key=") and bool(parsed.query[4:]))
    if channel == "serverchan":
        key = values["SERVERCHAN_SENDKEY"]
        return key.startswith("SCT") and key.isalnum()
    if channel == "qq_onebot":
        parsed = urlparse(values["QQ_ONEBOT_BASE"])
        target = values["QQ_GROUP_ID"] or values["QQ_USER_ID"]
        try:
            port = parsed.port
        except ValueError:
            return False
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and bool(port) and not parsed.path and not parsed.query
                and not parsed.fragment and not parsed.username and not parsed.password
                and bool(values["QQ_ONEBOT_TOKEN"]) and target.isdigit())
    return False


def save_notification_settings(values):
    """Replace only message keys; preserve monitor credentials and other settings."""
    env_file = ROOT / ".env"
    original = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    seen = set()
    output = []
    for line in original:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in MESSAGE_KEYS:
                if key not in seen:
                    output.append(f"{key}={values[key]}")
                    seen.add(key)
                continue
        output.append(line)
    for key in MESSAGE_KEYS:
        if key not in seen:
            output.append(f"{key}={values[key]}")
    temp = env_file.with_name(".env." + secrets.token_hex(8) + ".tmp")
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
        os.replace(temp, env_file)
        os.chmod(env_file, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def active_channels():
    values = notification_settings()
    if values["NOTIFY_ENABLED"] != "1":
        return []
    chosen = {part.strip() for part in values["NOTIFY_CHANNELS"].split(",")}
    return [channel for channel in CHANNEL_LABELS if channel in chosen and channel_ready(channel, values)]


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


def deliver(channel, title, body, manual_test=False):
    values = notification_settings()
    chosen = {part.strip() for part in values["NOTIFY_CHANNELS"].split(",")}
    if not channel_ready(channel, values):
        raise RuntimeError("消息通道参数尚不完整")
    if not manual_test and (values["NOTIFY_ENABLED"] != "1" or channel not in chosen):
        raise RuntimeError("消息通道已关闭")
    message = f"{title}\n{body}"[:1800]
    if channel == "wecom":
        url = values["WECOM_WEBHOOK_URL"]
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com"
                or parsed.path != "/cgi-bin/webhook/send" or not parsed.query.startswith("key=")):
            raise RuntimeError("企业微信 Webhook 地址格式不正确")
        result = _post(url, json={"msgtype": "text", "text": {"content": message}})
        if result.get("errcode") != 0:
            raise RuntimeError(f"企业微信拒绝消息，错误码 {result.get('errcode')}")
    elif channel == "serverchan":
        key = values["SERVERCHAN_SENDKEY"]
        if not key.startswith("SCT") or not key.isalnum():
            raise RuntimeError("Server酱 Turbo SendKey 格式不正确")
        result = _post(f"https://sctapi.ftqq.com/{key}.send",
                       data={"title": title[:100], "desp": body[:4000]})
        if result.get("code") != 0:
            raise RuntimeError(f"Server酱拒绝消息，错误码 {result.get('code')}")
    elif channel == "qq_onebot":
        base = values["QQ_ONEBOT_BASE"].rstrip("/")
        parsed = urlparse(base)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or not parsed.port or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("QQ 网关只能使用本机 127.0.0.1 HTTP 地址")
        group = values["QQ_GROUP_ID"].strip()
        user = values["QQ_USER_ID"].strip()
        target = group or user
        if not target.isdigit():
            raise RuntimeError("QQ 接收目标须为数字 ID")
        action = "send_group_msg" if group else "send_private_msg"
        field = "group_id" if group else "user_id"
        result = _post(f"{base}/{action}",
                       headers={"Authorization": "Bearer " + values["QQ_ONEBOT_TOKEN"]},
                       json={field: int(target), "message": message, "auto_escape": True})
        if result.get("status") != "ok" or result.get("retcode") != 0:
            raise RuntimeError(f"QQ 网关拒绝消息，错误码 {result.get('retcode')}")
    else:
        raise RuntimeError("未知消息通道")
