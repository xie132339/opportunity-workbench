"""Public, article-scoped Guangdiu detail-page parser."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup


_TITLE_MONEY = re.compile(
    r"(?:[¥￥]\s*([\d,]+(?:\.\d{1,2})?)(?![\d.])"
    r"|(?<![\d.])([\d,]+(?:\.\d{1,2})?)\s*元(?!\s*起))"
)


def _single_title_amount(title):
    """Preserve one explicit main-article headline amount as a claim only."""
    values = set()
    for match in _TITLE_MONEY.finditer(title or ''):
        try:
            amount = Decimal((match.group(1) or match.group(2)).replace(',', ''))
        except (InvalidOperation, AttributeError):
            continue
        cents = amount * 100
        if amount > 0 and cents == cents.to_integral_value():
            values.add(int(cents))
    return next(iter(values)) if len(values) == 1 else None


def supports_url(url):
    try:
        parsed = urlsplit(url or "")
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.scheme == "https" and parsed.hostname in {"guangdiu.com", "www.guangdiu.com"}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment and parsed.path == "/detail.php"
                and len(query.get("id", [])) == 1 and query["id"][0].isdigit())
    except ValueError:
        return False


def parse_detail(html):
    """Read only the main article title and its dabstract, never related-deal cards."""
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("#mainleft, .mainleft")
    if not main:
        raise ValueError("逛丢详情页缺少主内容区；未读取相关折扣或侧栏金额")
    abstract = main.select_one("#dabstract, .dabstract")
    if not abstract:
        raise ValueError("逛丢详情页缺少正文区域；未把页面推荐区作为商品条件")

    title = ""
    for link in main.select('a[href*="go.php"]'):
        candidate = " ".join(link.get_text(" ", strip=True).split())
        if candidate and not re.match(r"^(直达链接|前往购买|前往购买[»>])", candidate):
            title = candidate
            break
    if not title:
        heading = main.select_one("h1")
        title = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
    body = "\n".join(line.strip() for line in abstract.stripped_strings if line.strip())
    if not title or not body:
        raise ValueError("逛丢主商品标题或正文为空；未使用页面标题栏/推荐区金额代替")
    headline_amount = _single_title_amount(title)
    return {
        "title": title,
        "advertised_cents": None,
        "headline_amount_cents": headline_amount,
        "headline_amount_evidence": "逛丢详情主文章标题" if headline_amount is not None else "",
        "specification": "",
        "conditions": body[:12000],
        "published_at": None,
        "evidence": (title + "\n" + body)[:12000],
        "evidence_kind": "guangdiu_detail",
    }
