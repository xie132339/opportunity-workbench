"""Identify purchase marketplaces from canonical pages or validated official feed data."""
import re
from urllib.parse import parse_qs, urlsplit


FILTER_OPTIONS = (
    ("taobao", "淘宝系官方商品（淘宝/天猫）"),
    ("tmall", "天猫商品页"),
    ("jd", "京东商品页"),
    ("pdd", "拼多多商品页"),
    ("suning", "苏宁商品页"),
    ("vip", "唯品会商品页"),
    ("unknown", "商品页平台未确认"),
    ("all", "全部购买平台"),
)


def marketplace_matches_filter(marketplace_key, selected_key):
    """Treat the default Taobao selection as the Taobao ecosystem umbrella."""
    if selected_key == "all":
        return True
    if selected_key == "taobao":
        return marketplace_key in ("taobao", "tmall")
    return marketplace_key == selected_key


def _one_numeric_param(query, key):
    values = parse_qs(query).get(key, [])
    return len(values) == 1 and values[0].isdigit()


def marketplace_from_product_url(url):
    """Return a platform key only for an exact, recognized merchant item URL."""
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in (None, 443)):
        return None

    host, path, query = (parsed.hostname or "").lower(), parsed.path, parsed.query
    if host == "item.taobao.com" and path == "/item.htm" and _one_numeric_param(query, "id"):
        return "taobao"
    if (host in ("m.taobao.com", "h5.m.taobao.com")
            and path in ("/awp/core/detail.htm", "/item.htm")
            and _one_numeric_param(query, "id")):
        return "taobao"
    if (host in ("detail.tmall.com", "chaoshi.detail.tmall.com")
            and path == "/item.htm" and _one_numeric_param(query, "id")):
        return "tmall"
    if host == "item.jd.com" and re.fullmatch(r"/\d+\.html", path):
        return "jd"
    if (host == "item.m.jd.com" and path.startswith("/product/")
            and path.endswith(".html") and path[9:-5].isdigit()):
        return "jd"
    if (host == "mobile.yangkeduo.com" and path in ("/goods.html", "/goods2.html")
            and _one_numeric_param(query, "goods_id")):
        return "pdd"
    if host == "product.suning.com":
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].endswith(".html") and parts[1][:-5].isdigit():
            return "suning"
    if host == "detail.vip.com" and path.startswith("/detail-") and path.endswith(".html"):
        values = path[len("/detail-"):-len(".html")].split("-")
        if len(values) == 2 and all(value.isdigit() for value in values):
            return "vip"
    return None


def identify_product_marketplace(row):
    """Trust only product links produced by the canonical-link resolver.

    Text mentioning a marketplace, source publisher labels and affiliate URLs do
    not establish the purchase platform. A title mismatch from a read product
    page also blocks assignment.
    """
    labels = dict(FILTER_OPTIONS)
    links = row.get("product_links") or []
    matched = []
    for link in links:
        if not isinstance(link, dict) or link.get("title_alignment") == "different":
            continue
        key = marketplace_from_product_url(link.get("target_url"))
        if key:
            matched.append((key, link.get("target_url")))
    platforms = {key for key, _ in matched}
    if len(platforms) > 1:
        return {
            "marketplace_key": "multiple",
            "marketplace_label": "多个商品平台",
            "marketplace_evidence": "原文指向多个商城商品页，不能唯一归属。",
        }
    if not platforms:
        if row.get("source_method") == "taobao_alliance":
            from services.taobao_alliance import validated_offer
            offer = validated_offer(row.get("metadata_json"))
            if offer and offer.get("link_kind") == "official_promotion_link":
                key = offer.get("marketplace_key")
                if key not in ("taobao", "tmall"):
                    key = None
                label = "淘宝店（联盟推广入口）" if key == "taobao" else "天猫店（联盟推广入口）"
                return {
                    "marketplace_key": key or "unknown",
                    "marketplace_label": label if key else "未确认",
                    "marketplace_evidence": ("联盟官方接口返回商家类型和商品 ID；推广短链尚未独立解析为商品页，"
                                               "不证明同款或结算价。" if key else
                                               "联盟官方接口未返回可识别商家类型，不能按淘宝系商品归属。"),
                }
        return {
            "marketplace_key": "unknown",
            "marketplace_label": "未确认",
            "marketplace_evidence": "没有可确认的直达或有效解析商品页链接；标题/正文提及平台不作为依据。",
        }
    key = next(iter(platforms))
    url = next(url for item_key, url in matched if item_key == key)
    host = urlsplit(url).hostname
    return {
        "marketplace_key": key,
        "marketplace_label": labels[key],
        "marketplace_evidence": f"商品链接域名 {host}；只确认商城归属，不证明同款、价格、优惠或库存。",
    }


def attach_product_marketplace(row):
    row = dict(row)
    row.update(identify_product_marketplace(row))
    return row
