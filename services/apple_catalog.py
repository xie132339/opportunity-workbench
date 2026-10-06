"""Read the public price and exact part number from Apple China's refurb pages."""
import re
from decimal import Decimal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


_PRODUCT_PATH = re.compile(r"^/shop/product/([a-z0-9]+)/([a-z])$")
_PART_NUMBER = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]+/[A-Z])"')
_RMB_PRICE = re.compile(r"RMB\s+([\d,]+(?:\.\d{1,2})?)")
_CARD_RMB_PRICE = re.compile(r"(?<!\S)RMB\s+([\d,]+(?:\.\d{1,2})?)(?![\d.,])")


def supports_product_url(url):
    parsed = urlsplit(url or "")
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "www.apple.com.cn"
        and parsed.port in (None, 443)
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment
        and _PRODUCT_PATH.fullmatch(parsed.path)
    )


def parse_listing_card(url, title, card_text, listing_url):
    """Extract a first-party refurb price only from the linked product's own card."""
    parsed = urlsplit(url or "")
    match = _PRODUCT_PATH.fullmatch(parsed.path)
    title = " ".join((title or "").split())
    card_text = " ".join((card_text or "").split())
    if not supports_product_url(url) or not match:
        raise ValueError("目录卡片没有受支持的 Apple 中国商品链接")
    if not title or title not in card_text:
        raise ValueError("目录卡片标题与商品链接未能绑定")

    prices = _CARD_RMB_PRICE.findall(card_text)
    if len(prices) != 1:
        raise ValueError("目录卡片没有唯一的人民币公开价签")
    amount = Decimal(prices[0].replace(",", ""))
    cents = amount * 100
    if cents != cents.to_integral_value() or cents <= 0:
        raise ValueError("目录卡片公开价签不是有效的人民币金额")

    part_number = f"{match[1].upper()}/{match[2].upper()}"
    evidence = (
        f"官方目录页：{listing_url}\n"
        f"目录卡片原文：{card_text}\n"
        f"Apple 商品编号（由商品链接路径提取）：{part_number}\n"
        f"人民币公开价签：RMB {amount:,.2f}\n"
        "证据口径：Apple 中国官方翻新目录卡片公开标价；不是账号结算价。"
    )
    return {
        "title": title,
        "advertised_cents": int(cents),
        "specification": f"Apple 商品编号 {part_number}",
        "product_identity": f"apple:{part_number}",
        "currency": "CNY",
        "conditions": "Apple 中国官方翻新目录卡片公开标价；目录未证明详情页库存、运费、账号资格或最终结算金额。",
        "evidence": evidence,
        "evidence_kind": "apple_catalog_card",
        "source_url": listing_url,
        "card_text": card_text,
        "published_at": None,
    }


def parse_product_detail(url, html):
    """Return an auditable public catalogue observation; fail closed on drift."""
    parsed = urlsplit(url or "")
    match = _PRODUCT_PATH.fullmatch(parsed.path)
    if not supports_product_url(url) or not match:
        raise ValueError("不是受支持的 Apple 中国翻新商品页")

    part_number = f"{match[1].upper()}/{match[2].upper()}"
    page_part_numbers = {value.upper() for value in _PART_NUMBER.findall(html or "")}
    if part_number not in page_part_numbers:
        raise ValueError("商品页结构中没有与链接对应的 Apple 商品编号")

    soup = BeautifulSoup(html or "", "html.parser")
    heading = soup.select_one("h1")
    title = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
    if not title:
        raise ValueError("Apple 商品页没有明确商品标题")

    price_texts = [" ".join(node.get_text(" ", strip=True).split())
                   for node in soup.select(".rf-pdp-currentprice")]
    amounts = set()
    for price_text in price_texts:
        price_match = _RMB_PRICE.fullmatch(price_text)
        if not price_match:
            raise ValueError("Apple 当前价签格式不明确，未提取价格")
        amount = Decimal(price_match[1].replace(",", ""))
        cents = amount * 100
        if cents != cents.to_integral_value() or cents <= 0:
            raise ValueError("Apple 当前公开标价不是有效的人民币金额")
        amounts.add(int(cents))
    if len(amounts) != 1:
        raise ValueError("Apple 商品页缺唯一的 RMB 当前标价")

    amount_cents = next(iter(amounts))
    amount = Decimal(amount_cents) / 100
    evidence = (
        f"商品页标题：{title}\n"
        f"Apple 商品编号：{part_number}\n"
        f"公开价签选择器 .rf-pdp-currentprice：RMB {amount:,.2f}\n"
        "证据口径：Apple 中国官方翻新商品页公开标价；不是账号结算价。"
    )
    return {
        "title": title,
        "advertised_cents": amount_cents,
        "specification": f"Apple 商品编号 {part_number}",
        "product_identity": f"apple:{part_number}",
        "currency": "CNY",
        "conditions": "Apple 中国官方翻新商城目录公开标价；本次读取未核验库存、运费、账号资格及结算页金额。",
        "evidence": evidence,
        "published_at": None,
    }
