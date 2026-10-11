"""Maintainable official Taobao Alliance topics for everyday household goods.

A topic is deliberately one existing ``sources`` row, rather than a second
configuration table.  That keeps the query, schedule, collection evidence and
pause state together and makes old observations auditable after a topic is
retired.
"""
from dataclasses import dataclass
from urllib.parse import urlencode

from services.taobao_alliance import API_ENDPOINT, configuration_error, search_config


TOPIC_SOURCE_PREFIX = "淘宝生活品·"
DEFAULT_INTERVAL_MINUTES = 60
SOURCE_CATEGORY = "零售优惠"


@dataclass(frozen=True)
class LivingTopic:
    key: str
    group: str
    name: str
    keyword: str


CORE_LIVING_TOPICS = (
    LivingTopic("paper", "生活必需品", "纸品", "抽纸"),
    LivingTopic("water", "生活必需品", "饮用水", "饮用水"),
    LivingTopic("rice", "生活必需品", "大米", "大米"),
    LivingTopic("flour", "生活必需品", "面粉", "面粉"),
    LivingTopic("cooking_oil", "生活必需品", "食用油", "食用油"),
    LivingTopic("laundry_detergent", "居家清洁", "洗衣液", "洗衣液"),
)


def normalize_keyword(value):
    """Return a stable search phrase accepted by the official adapter."""
    keyword = " ".join(str(value or "").split())
    if not keyword:
        raise ValueError("生活品搜索关键词不能为空")
    if len(keyword) > 100:
        raise ValueError("生活品搜索关键词不能超过 100 个字符")
    # Delegate the final validation to the same contract used by the collector.
    search_config(source_url(keyword))
    return keyword


def source_url(keyword):
    """Build the only source URL form used by managed living topics."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        raise ValueError("生活品搜索关键词不能为空")
    return API_ENDPOINT + "?" + urlencode((
        ("q", keyword),
        ("page_no", "1"),
        ("page_size", "100"),
        ("max_pages", "all"),
    ))


def topic_keyword(source):
    """Return an official source's query phrase, or ``None`` for other rows."""
    if not source or source["method"] != "taobao_alliance":
        return None
    try:
        return search_config(source["url"])["q"]
    except (KeyError, TypeError, ValueError):
        return None


def _topic_name(topic):
    return f"{TOPIC_SOURCE_PREFIX}{topic.name}优惠搜索"


def _source_rows_by_keyword(rows):
    """Pick one auditable source per query, without creating a false merge."""
    grouped = {}
    for row in rows:
        keyword = topic_keyword(row)
        if keyword is None:
            continue
        grouped.setdefault(keyword, []).append(row)
    # A raw source id is stable and makes a duplicate configuration obvious in
    # the UI.  We never delete or silently merge existing source histories.
    return {keyword: sorted(matches, key=lambda item: item["id"])
            for keyword, matches in grouped.items()}


def configurations(rows):
    """Project source rows into default and user-created topic configurations."""
    by_keyword = _source_rows_by_keyword(rows)
    result = []
    known_keywords = set()
    for topic in CORE_LIVING_TOPICS:
        matches = by_keyword.get(topic.keyword, [])
        source = matches[0] if matches else None
        result.append({
            "key": topic.key,
            "group": topic.group,
            "name": topic.name,
            "keyword": topic.keyword,
            "source": source,
            "source_id": source["id"] if source else None,
            "duplicate_source_ids": [item["id"] for item in matches[1:]],
            "default": True,
        })
        known_keywords.add(topic.keyword)
    for keyword, matches in sorted(by_keyword.items()):
        managed = [item for item in matches if item["name"].startswith(TOPIC_SOURCE_PREFIX)]
        if keyword in known_keywords or not managed:
            continue
        source = managed[0]
        result.append({
            "key": f"custom-{source['id']}",
            "group": "自定义生活品",
            "name": source["name"].removeprefix(TOPIC_SOURCE_PREFIX).removesuffix("优惠搜索") or keyword,
            "keyword": keyword,
            "source": source,
            "source_id": source["id"],
            "duplicate_source_ids": [item["id"] for item in matches if item["id"] != source["id"]],
            "default": False,
        })
    return result


def _status_for_new_source(env):
    return "not_configured" if configuration_error(env) else "pending"


def _insert_source(db, *, name, keyword, interval_minutes, env, enabled=True):
    cursor = db.execute("""INSERT INTO sources
        (platform,name,category,url,method,parser,status,enabled,interval_minutes)
        VALUES(?,?,?,?,?,?,?,?,?)""", (
            "淘宝联盟", name, SOURCE_CATEGORY, source_url(keyword),
            "taobao_alliance", "taobao_alliance", _status_for_new_source(env),
            int(bool(enabled)), interval_minutes,
        ))
    return cursor.lastrowid


def ensure_core_topics(db, *, env, enabled=True):
    """Create only missing core topics and adopt exact existing official queries."""
    rows = db.execute("SELECT * FROM sources WHERE method='taobao_alliance' ORDER BY id").fetchall()
    existing = _source_rows_by_keyword(rows)
    created, adopted = [], []
    for topic in CORE_LIVING_TOPICS:
        matches = existing.get(topic.keyword, [])
        if matches:
            adopted.append({"topic": topic, "source_id": matches[0]["id"]})
            continue
        source_id = _insert_source(db, name=_topic_name(topic), keyword=topic.keyword,
                                   interval_minutes=DEFAULT_INTERVAL_MINUTES,
                                   env=env, enabled=enabled)
        created.append({"topic": topic, "source_id": source_id})
    return {"created": created, "adopted": adopted}


def add_custom_topic(db, *, name, keyword, interval_minutes, env, enabled=True):
    """Create one immutable-query source for a user-maintained living topic."""
    name = " ".join(str(name or "").split())
    if not 2 <= len(name) <= 32:
        raise ValueError("主题名称须为 2 到 32 个字符")
    keyword = normalize_keyword(keyword)
    if not DEFAULT_INTERVAL_MINUTES // 4 <= interval_minutes <= 1440:
        raise ValueError("淘宝生活品检查间隔须在 15 到 1440 分钟之间")
    existing = db.execute("SELECT id FROM sources WHERE method='taobao_alliance'").fetchall()
    for row in existing:
        source = db.execute("SELECT * FROM sources WHERE id=?", (row["id"],)).fetchone()
        if topic_keyword(source) == keyword:
            raise ValueError(f"关键词“{keyword}”已由来源 #{source['id']} 维护；请直接调整其启停或频率")
    source_id = _insert_source(db, name=f"{TOPIC_SOURCE_PREFIX}{name}优惠搜索",
                               keyword=keyword, interval_minutes=interval_minutes,
                               env=env, enabled=enabled)
    return source_id
