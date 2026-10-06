"""Fail-closed public price extraction for supported first-party product pages."""
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


_PATHS = {
    "item.jd.com": re.compile(r"^/\d+\.html$"),
    "item.m.jd.com": re.compile(r"^/product/\d+\.html$"),
    "item.taobao.com": re.compile(r"^/item\.htm$"),
    "detail.tmall.com": re.compile(r"^/item\.htm$"),
    "chaoshi.detail.tmall.com": re.compile(r"^/item\.htm$"),
    "mobile.yangkeduo.com": re.compile(r"^/(?:goods|goods2)\.html$"),
    "detail.vip.com": re.compile(r"^/detail-\d+-\d+\.html$"),
    "product.suning.com": re.compile(r"^/\d+/\d+\.html$"),
    "item.lenovo.com.cn": re.compile(r"^/product/\d+\.html$"),
    "www.honor.com": re.compile(r"^/cn/shop/product/\d+\.html$"),
    "book.kongfz.com": re.compile(r"^/\d+/\d+/?$"),
}
_MARKETPLACE_QUERY_IDS = {
    "item.taobao.com": ("id", "taobao"),
    "detail.tmall.com": ("id", "taobao"),
    "chaoshi.detail.tmall.com": ("id", "taobao"),
    "mobile.yangkeduo.com": ("goods_id", "pdd"),
}
_STRUCTURED_MARKETPLACE_HOSTS = frozenset({
    "item.jd.com", "item.m.jd.com", "item.taobao.com", "detail.tmall.com",
    "chaoshi.detail.tmall.com", "mobile.yangkeduo.com", "detail.vip.com",
})


def supports_url(url):
    """Limit direct-page crawling to the already configured merchant hosts."""
    try:
        parsed = urlsplit(url or "")
        port = parsed.port
        if (parsed.scheme != "https" or port not in (None, 443)
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment):
            return False
    except ValueError:
        return False
    host = parsed.hostname.lower()
    if host == "www.mi.com":
        query = parse_qs(parsed.query, keep_blank_values=True)
        product_ids = query.get("product_id", [])
        tracking = query.get("cfrom", [])
        return (parsed.path in {"/shop/buy", "/shop/buy/detail", "/buy/detail"}
                and len(product_ids) == 1 and product_ids[0].isdigit()
                and set(query) <= {"product_id", "cfrom"}
                and (not tracking or (len(tracking) == 1
                     and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", tracking[0]))))
    if host == "www.honor.com":
        query = parse_qs(parsed.query, keep_blank_values=True)
        campaign_ids = query.get("cid", [])
        return (bool(_PATHS[host].fullmatch(parsed.path))
                and set(query) <= {"cid"}
                and (not campaign_ids or (len(campaign_ids) == 1 and campaign_ids[0].isdigit())))
    if host in _MARKETPLACE_QUERY_IDS:
        key, _ = _MARKETPLACE_QUERY_IDS[host]
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (bool(_PATHS[host].fullmatch(parsed.path))
                and set(query) == {key} and len(query[key]) == 1
                and query[key][0].isdigit())
    pattern = _PATHS.get(host)
    return bool(pattern and pattern.fullmatch(parsed.path) and not parsed.query)


def _types(value):
    if isinstance(value, str):
        return {part.rsplit("/", 1)[-1].rsplit(":", 1)[-1].casefold()
                for part in re.split(r"\s+", value) if part}
    if isinstance(value, list):
        return set().union(*(_types(part) for part in value)) if value else set()
    return set()


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _same_product_page(candidate, page_url):
    try:
        candidate_url = urlsplit(urljoin(page_url, candidate or ""))
        page = urlsplit(page_url)
    except ValueError:
        return False
    if (candidate_url.hostname or "").lower() != (page.hostname or "").lower():
        return False
    host = (page.hostname or "").lower()
    if host in _MARKETPLACE_QUERY_IDS:
        key, _ = _MARKETPLACE_QUERY_IDS[host]
        return (parse_qs(candidate_url.query).get(key)
                == parse_qs(page.query).get(key)
                and len(parse_qs(page.query).get(key, [])) == 1)
    if host == "item.jd.com":
        return bool(re.fullmatch(r"/\d+\.html", candidate_url.path)
                    and candidate_url.path == page.path)
    if host == "item.m.jd.com":
        return bool(re.fullmatch(r"/product/\d+\.html", candidate_url.path)
                    and candidate_url.path == page.path)
    if host == "detail.vip.com":
        return bool(re.fullmatch(r"/detail-\d+-\d+\.html", candidate_url.path)
                    and candidate_url.path == page.path)
    if page.hostname == "www.mi.com":
        if candidate_url.path not in {"/shop/buy", "/shop/buy/detail", "/buy/detail"}:
            return False
        return (parse_qs(candidate_url.query).get("product_id")
                == parse_qs(page.query).get("product_id"))
    return candidate_url.path.rstrip("/") == page.path.rstrip("/")


def _marketplace_page_identity(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "item.jd.com":
        match = re.fullmatch(r"/(\d+)\.html", parsed.path)
        return f"jd:{match[1]}" if match else ""
    if host == "item.m.jd.com":
        match = re.fullmatch(r"/product/(\d+)\.html", parsed.path)
        return f"jd:{match[1]}" if match else ""
    if host in _MARKETPLACE_QUERY_IDS:
        key, prefix = _MARKETPLACE_QUERY_IDS[host]
        values = parse_qs(parsed.query).get(key, [])
        return f"{prefix}:{values[0]}" if len(values) == 1 and values[0].isdigit() else ""
    if host == "detail.vip.com":
        match = re.fullmatch(r"/detail-(\d+)-(\d+)\.html", parsed.path)
        return f"vip:{match[1]}:{match[2]}" if match else ""
    return ""


def _marketplace_metadata_matches(soup, page_url):
    if (urlsplit(page_url).hostname or "").lower() not in _STRUCTURED_MARKETPLACE_HOSTS:
        return True
    nodes = [soup.select_one('meta[property="og:url"][content]'),
             soup.select_one('link[rel="canonical"][href]')]
    return any(node and _same_product_page(node.get("content") or node.get("href"), page_url)
               for node in nodes)


def _cents(value):
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    cents = amount * 100
    if amount <= 0 or cents != cents.to_integral_value():
        return None
    return int(cents)


def _offer_price(offer):
    """Only accept one active Offer, never an AggregateOffer/range or coupon."""
    if not isinstance(offer, dict):
        return None
    kinds = _types(offer.get("@type"))
    if "aggregateoffer" in kinds or "lowPrice" in offer or "highPrice" in offer:
        return None
    if kinds and "offer" not in kinds:
        return None
    currency = str(offer.get("priceCurrency") or "").upper()
    if currency not in {"CNY", "RMB"}:
        return None
    availability = str(offer.get("availability") or "").casefold()
    if any(value in availability for value in ("outofstock", "discontinued", "soldout")):
        return None
    cents = _cents(offer.get("price"))
    if cents is None:
        return None
    return cents, currency, availability


def _product_url(product):
    return product.get("url") or product.get("@id") or ""


def _schema_products(soup):
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        products.extend(node for node in _walk(payload)
                        if "product" in _types(node.get("@type")))
    return products


def _microdata_product(soup):
    scopes = [node for node in soup.select("[itemscope][itemtype]")
              if "product" in _types(node.get("itemtype"))]
    outer = [node for node in scopes
             if not any("product" in _types(parent.get("itemtype"))
                        for parent in node.parents if getattr(parent, "attrs", None))]
    return outer[0] if len(outer) == 1 else None


def _microdata_value(scope, name):
    node = scope.select_one(f'[itemprop="{name}"]')
    if not node:
        return ""
    return (node.get("content") or node.get("value") or node.get("href")
            or node.get("datetime") or node.get("data-value")
            or node.get_text(" ", strip=True))


def _product_meta_price(soup):
    """Read document-level Open Graph Product price tags, not arbitrary DOM numbers."""
    amounts = []
    currencies = []
    availability = []
    for node in soup.select("meta[property], meta[name]"):
        key = str(node.get("property") or node.get("name") or "").casefold()
        value = str(node.get("content") or "").strip()
        if key in {"product:price:amount", "og:price:amount"} and value:
            amounts.append(value)
        elif key in {"product:price:currency", "og:price:currency"} and value:
            currencies.append(value.upper())
        elif key in {"product:availability", "og:availability"} and value:
            availability.append(value.casefold())
    parsed_amounts = [_cents(value) for value in amounts]
    values = set(parsed_amounts)
    currency_values = set(currencies)
    stocks = set(availability)
    if (not amounts or any(value is None for value in parsed_amounts) or len(values) != 1
            or currency_values not in ({"CNY"}, {"RMB"}) or len(stocks) > 1):
        return None
    stock = next(iter(stocks), "")
    if any(value in stock for value in ("outofstock", "discontinued", "soldout")):
        return None
    return next(iter(values)), stock


def _result(title, amount, sku, page_url, price_evidence, availability="",
            page_amount_cents=None, page_amount_label="", page_amount_evidence="",
            availability_status="", product_identity=""):
    if amount is None:
        note = ("商品页明确显示已下架；拒绝使用主价区外的残留价格。" if availability_status == "out_of_stock"
                else "商品页未提供唯一、可绑定本商品的人民币公开标价；保留页面线索，不把推荐区、券额或多规格区间当商品价。")
        evidence = f"商品页标题：{title}\n读取页面：{page_url}\n结果：{note}"
        conditions = note
    else:
        amount_text = f"¥{Decimal(amount) / 100:.2f}"
        note = "来源商品页公开标价；不是账号结算价，未证明库存、运费、优惠资格或最终付款金额。"
        evidence = (f"商品页标题：{title}\n读取页面：{page_url}\n"
                    f"结构化价格依据：{price_evidence}，人民币公开标价 {amount_text}。")
        conditions = note + (f" 页面标示库存状态：{availability}." if availability else "")
    if page_amount_cents is not None:
        amount_text = f"¥{Decimal(page_amount_cents) / 100:.2f}"
        page_amount_evidence = page_amount_evidence or "商家页面价格字段"
        evidence += f"\n另见{page_amount_label} {amount_text}（{page_amount_evidence}）；保留为页面金额线索，不作为公开标价。"
        conditions += f" 页面金额线索：{page_amount_label} {amount_text}；不是可执行整单报价。"
    return {
        "title": title,
        "advertised_cents": amount,
        "specification": (f"商品编号 {sku}" if sku else ""),
        "product_identity": product_identity or sku,
        "currency": "CNY" if amount is not None else "",
        "conditions": conditions,
        "evidence": evidence,
        "evidence_kind": "merchant_product_page",
        "price_evidence": price_evidence if amount is not None else "",
        "page_amount_cents": page_amount_cents,
        "page_amount_label": page_amount_label,
        "page_amount_evidence": page_amount_evidence,
        "availability_status": availability_status,
        "published_at": None,
    }


def _suning_unavailable(soup):
    main = soup.select_one("#priceDom")
    if not main:
        return False
    text = " ".join(main.get_text(" ", strip=True).split())
    return bool(re.search(r"此商品已下架|商品已下架|商品已售罄|已售罄", text))


def _suning_main_price(soup):
    """Only parse a single currency amount inside Suning's current main-price block."""
    main = soup.select_one("#priceDom #mainPrice")
    if not main:
        return None
    text = " ".join(main.get_text(" ", strip=True).split())
    matches = re.findall(r"[¥￥]\s*([\d,]+(?:\.\d{1,2})?)", text)
    values = {_cents(value) for value in matches}
    values.discard(None)
    return next(iter(values)) if len(values) == 1 and len(matches) == 1 else None


def _lenovo_main_price(soup, selector):
    node = soup.select_one(selector)
    if not node:
        return None
    raw = (node.get("content") or node.get("value") or node.get_text(" ", strip=True)).strip()
    if not raw:
        return None
    matches = re.findall(r"(?:[¥￥]\s*)?([\d,]+(?:\.\d{1,2})?)", raw)
    values = {_cents(value) for value in matches}
    values.discard(None)
    # These IDs are specifically the single price slot for the product page.
    # Multiple numbers still mean an ambiguous component or stale placeholder.
    return next(iter(values)) if len(values) == 1 and len(matches) == 1 else None


def parse_product_page(url, html):
    """Parse public Product/Offer fields; dynamic or ambiguous prices stay empty."""
    if not supports_url(url):
        raise ValueError("商品页域名或路径不在已配置的公开来源白名单")
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    amount = None
    sku = ""
    availability = ""
    evidence_kind = ""
    page_amount_cents = None
    page_amount_label = ""
    page_amount_evidence = ""
    availability_status = ""

    suning_unavailable = urlsplit(url).hostname == "product.suning.com" and _suning_unavailable(soup)
    if suning_unavailable:
        availability_status = "out_of_stock"

    products = _schema_products(soup)
    page_products = [item for item in products
                     if _product_url(item) and _same_product_page(_product_url(item), url)]
    # A mismatched Product URL is likely a carousel/recommendation and must not
    # be used as the current page's price. URL-less JSON-LD is accepted only
    # when the document contains exactly one Product node.
    url_less_products = [item for item in products if not _product_url(item)]
    structured_marketplace = (urlsplit(url).hostname or "").lower() in _STRUCTURED_MARKETPLACE_HOSTS
    product = (page_products[0] if len(page_products) == 1
               else url_less_products[0] if not page_products and len(products) == 1
               and len(url_less_products) == 1 and not structured_marketplace else None)
    if product and not suning_unavailable:
        title = str(product.get("name") or "").strip()
        sku = str(product.get("sku") or product.get("mpn") or "").strip()
        raw_offers = product.get("offers") or []
        if isinstance(raw_offers, dict):
            raw_offers = [raw_offers]
        if isinstance(raw_offers, list):
            exact = [parsed for parsed in (_offer_price(item) for item in raw_offers)
                     if parsed is not None]
            if len(exact) == 1:
                amount, _, availability = exact[0]
                evidence_kind = "schema.org Product/Offer"

    if not evidence_kind and not suning_unavailable:
        scope = _microdata_product(soup)
        if scope:
            title = _microdata_value(scope, "name") or title
            sku = _microdata_value(scope, "sku") or _microdata_value(scope, "mpn") or sku
            raw_offers = [node for node in scope.select('[itemprop="offers"][itemscope]')
                          if "aggregateoffer" not in _types(node.get("itemtype"))]
            exact = []
            for node in raw_offers:
                candidate = {
                    "@type": node.get("itemtype", ""),
                    "price": _microdata_value(node, "price"),
                    "priceCurrency": _microdata_value(node, "priceCurrency"),
                    "availability": _microdata_value(node, "availability"),
                }
                parsed = _offer_price(candidate)
                if parsed is not None:
                    exact.append(parsed)
            if len(exact) == 1:
                amount, _, availability = exact[0]
                evidence_kind = "schema.org Microdata Product/Offer"

    # Some merchant pages publish the exact current product price as document
    # metadata while omitting JSON-LD/Microdata. Do not use this fallback when
    # the document has multiple Product nodes: their variants/offers are ambiguous.
    metadata_matches = _marketplace_metadata_matches(soup, url)
    if not evidence_kind and not products and not suning_unavailable and metadata_matches:
        meta_price = _product_meta_price(soup)
        if meta_price:
            amount, availability = meta_price
            evidence_kind = "Open Graph Product price metadata"

    host = urlsplit(url).hostname
    if not evidence_kind and not suning_unavailable and host == "product.suning.com":
        amount = _suning_main_price(soup)
        if amount is not None:
            evidence_kind = "苏宁商品主价区 #priceDom #mainPrice"

    if not evidence_kind and not suning_unavailable and host == "item.lenovo.com.cn":
        amount = _lenovo_main_price(soup, "#span_price")
        if amount is not None:
            evidence_kind = "联想商城商品页 #span_price"
        else:
            page_amount_cents = _lenovo_main_price(soup, "#estimatedPrice-span_price")
            if page_amount_cents is not None:
                page_amount_label = "联想商城页面预估到手价"
                page_amount_evidence = "DOM #estimatedPrice-span_price"

    if not evidence_kind and not suning_unavailable and host == "www.honor.com":
        label_node = soup.select_one("#pro-price-hand")
        estimate_node = soup.select_one("#pro-price-hide")
        label = " ".join(label_node.get_text(" ", strip=True).split()) if label_node else ""
        if estimate_node and re.search(r"预估到手价|预计到手价", label):
            candidate = _cents(estimate_node.get("value"))
            if candidate is not None:
                page_amount_cents = candidate
                page_amount_label = "荣耀商城页面预估到手价"
                page_amount_evidence = "DOM #pro-price-hand 标签 + #pro-price-hide 值"

    if host == "www.mi.com":
        canonical = soup.select_one('link[rel="canonical"][href]')
        if canonical and not _same_product_page(canonical.get("href", ""), url):
            raise ValueError("小米页面规范商品ID与请求链接不一致；不采纳跳转后另一商品的金额")

    if not title:
        heading = soup.select_one("h1")
        title = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
    if not title and metadata_matches:
        og_title = soup.select_one('meta[property="og:title"][content]')
        if og_title:
            title = " ".join(og_title.get("content", "").split())
    if not title and soup.title:
        title = " ".join(soup.title.get_text(" ", strip=True).split())
        title = re.sub(r"\s*(?:立即购买|小米商城|荣耀商城|联想商城).*?$", "", title).strip(" -_|")
    host = (urlsplit(url).hostname or "").lower()
    generic_marketplace_title = (host in _STRUCTURED_MARKETPLACE_HOSTS and
                                 (title in {"京东", "拼多多", "淘宝", "淘宝网", "天猫", "京东(JD.COM)"}
                                  or title.startswith("京东(JD.COM)-正品低价")))
    if (not title or generic_marketplace_title
            or re.search(r"登录|安全验证|验证码|访问受限|人机验证", title)):
        raise ValueError("商品页未取得可确认的商品标题；页面可能是动态壳、验证页或登录页")
    return _result(title, amount, sku, url, evidence_kind, availability,
                   page_amount_cents, page_amount_label, page_amount_evidence,
                   availability_status, _marketplace_page_identity(url))
