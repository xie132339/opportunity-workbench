"""SQLite storage for the local opportunity workbench."""
from contextlib import contextmanager
from pathlib import Path
import os
import json
import sqlite3
from offer import detected_offer_type, TOPIC_LABELS
from services.category_policy import default_policy, encode_policy
from services.category_comparison_rules import default_rule, encode_rule

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WORKBENCH_DB", ROOT / "data" / "workbench.sqlite3"))


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL,
 category TEXT NOT NULL, url TEXT NOT NULL, method TEXT NOT NULL,
 parser TEXT NOT NULL DEFAULT '', watch_uuid TEXT,
 status TEXT NOT NULL DEFAULT 'pending', enabled INTEGER NOT NULL DEFAULT 1,
 interval_minutes INTEGER NOT NULL DEFAULT 60,
 last_checked TEXT, last_success TEXT, last_error TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(platform, url, method)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
 external_key TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
 snippet TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 published_at TEXT, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 is_baseline INTEGER NOT NULL DEFAULT 0,
 UNIQUE(source_id, external_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS opportunities (
 id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE REFERENCES events(id),
 source_id INTEGER REFERENCES sources(id), title TEXT NOT NULL, category TEXT NOT NULL,
 url TEXT, notes TEXT NOT NULL DEFAULT '', specification TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', buy_cents INTEGER,
 resource_kind TEXT NOT NULL DEFAULT 'unknown', topic TEXT NOT NULL DEFAULT 'other',
 offer_type TEXT NOT NULL DEFAULT 'unknown', eligibility TEXT NOT NULL DEFAULT 'unknown',
 purchase_limit INTEGER,
 buy_checked_at TEXT, buy_proof TEXT NOT NULL DEFAULT '',
 buy_shipping_cents INTEGER, sell_shipping_cents INTEGER,
 platform_fee_cents INTEGER, processing_cents INTEGER,
 other_cents INTEGER, reserve_cents INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotes (
 id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 kind TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 offer_type TEXT NOT NULL DEFAULT 'unknown',
 specification TEXT NOT NULL DEFAULT '', conditions TEXT NOT NULL DEFAULT '',
 same_spec INTEGER NOT NULL DEFAULT 0, final_quote INTEGER NOT NULL DEFAULT 0,
 evidence_url TEXT, price_at TEXT, valid_until TEXT,
 observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS benchmark_categories (
 id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES benchmark_categories(id),
 name TEXT NOT NULL UNIQUE, pricing_unit TEXT NOT NULL, identity_rule TEXT NOT NULL,
 topic_key TEXT NOT NULL DEFAULT 'other', match_terms_json TEXT NOT NULL DEFAULT '[]',
 policy_json TEXT NOT NULL DEFAULT '{}', comparison_rule_json TEXT NOT NULL DEFAULT '{}',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS benchmark_category_rule_audit (
 id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, version INTEGER NOT NULL,
 previous_policy_json TEXT, policy_json TEXT NOT NULL,
 changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS benchmark_topic_rules (
 topic_key TEXT PRIMARY KEY, policy_json TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS benchmark_topic_rule_audit (
 id INTEGER PRIMARY KEY, topic_key TEXT NOT NULL, version INTEGER NOT NULL,
 previous_policy_json TEXT, policy_json TEXT NOT NULL,
 changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS benchmark_providers (
 id INTEGER PRIMARY KEY, provider_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 kind TEXT NOT NULL, scope TEXT NOT NULL, access_state TEXT NOT NULL DEFAULT 'not_connected',
 evidence_url TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', watched INTEGER NOT NULL DEFAULT 1 CHECK(watched IN (0,1)),
 sample_count INTEGER NOT NULL DEFAULT 0 CHECK(sample_count >= 0), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS buy_checks (
 id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 specification TEXT NOT NULL, buy_cents INTEGER NOT NULL,
 shipping_cents INTEGER NOT NULL, proof TEXT NOT NULL,
 checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS strategies (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, include_words TEXT NOT NULL DEFAULT '',
 exclude_words TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
 max_buy_cents INTEGER, min_profit_cents INTEGER, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE NOT NULL REFERENCES events(id),
 opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 channel TEXT NOT NULL DEFAULT 'in_app', status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sent_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
 id INTEGER PRIMARY KEY, notification_id INTEGER NOT NULL REFERENCES notifications(id),
 channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 sent_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(notification_id, channel)
);
CREATE TABLE IF NOT EXISTS trades (
 id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 state TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
 buy_cents INTEGER NOT NULL, buy_fees_cents INTEGER NOT NULL DEFAULT 0,
 sale_cents INTEGER, sale_fees_cents INTEGER,
 refund_cents INTEGER NOT NULL DEFAULT 0, deposit_cents INTEGER NOT NULL DEFAULT 0,
 deposit_lost_cents INTEGER NOT NULL DEFAULT 0,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status, created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON notification_deliveries(status, id);
CREATE INDEX IF NOT EXISTS idx_buy_checks_opp ON buy_checks(opportunity_id, checked_at);
CREATE TABLE IF NOT EXISTS auto_reviews (
 opportunity_id INTEGER PRIMARY KEY REFERENCES opportunities(id),
 state TEXT NOT NULL, reason TEXT NOT NULL,
 advertised_cents INTEGER, specification TEXT NOT NULL DEFAULT '',
 conditions TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
 checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 detail_checked_at TEXT, detail_json TEXT, detail_error TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_check_at TEXT
);
CREATE TABLE IF NOT EXISTS benefit_observations (
 id INTEGER PRIMARY KEY, resource_url TEXT NOT NULL, checked_at TEXT NOT NULL,
 method TEXT NOT NULL, evidence_json TEXT NOT NULL,
 UNIQUE(resource_url,checked_at,method)
);
CREATE TABLE IF NOT EXISTS benefit_resources (
 id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
 source_type TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '', source_opportunity_id INTEGER,
 origin_text TEXT NOT NULL DEFAULT '', published_at TEXT,
 first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 target_url TEXT, evidence_json TEXT NOT NULL DEFAULT '{}', checked_at TEXT, next_check_at TEXT
);
CREATE TABLE IF NOT EXISTS benefit_resource_sources (
 id INTEGER PRIMARY KEY, resource_id INTEGER NOT NULL REFERENCES benefit_resources(id),
 source_key TEXT NOT NULL, source_type TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
 source_opportunity_id INTEGER, source_title TEXT NOT NULL DEFAULT '',
 origin_text TEXT NOT NULL DEFAULT '', published_at TEXT,
 first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(resource_id,source_key)
);
CREATE TABLE IF NOT EXISTS benefit_product_relations (
 id INTEGER PRIMARY KEY,
 resource_id INTEGER NOT NULL REFERENCES benefit_resources(id),
 opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 state TEXT NOT NULL DEFAULT 'source_linked',
 basis TEXT NOT NULL DEFAULT '', evidence_url TEXT NOT NULL DEFAULT '',
 checked_at TEXT, valid_until TEXT, stackable TEXT NOT NULL DEFAULT 'unknown',
 UNIQUE(resource_id,opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_benefit_product_opportunity
 ON benefit_product_relations(opportunity_id,state,resource_id);
CREATE TRIGGER IF NOT EXISTS benefit_source_product_relation
AFTER INSERT ON benefit_resource_sources
WHEN NEW.source_opportunity_id IS NOT NULL
BEGIN
 INSERT OR IGNORE INTO benefit_product_relations
  (resource_id,opportunity_id,state,basis,evidence_url)
 VALUES(NEW.resource_id,NEW.source_opportunity_id,'source_linked',
        '优惠入口与商品出现在同一条来源记录中',NEW.source_url);
END;
CREATE TABLE IF NOT EXISTS link_resolutions (
 url TEXT PRIMARY KEY, target_url TEXT, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 checked_at TEXT NOT NULL, next_check_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_runs (
 id INTEGER PRIMARY KEY CHECK(id=1), started_at TEXT, finished_at TEXT,
 reviewed INTEGER NOT NULL DEFAULT 0, probed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_candidates (
 id INTEGER PRIMARY KEY,
 platform TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
 url TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'rsshub', parser TEXT NOT NULL DEFAULT '',
 discovery_provider TEXT NOT NULL, discovery_repo TEXT NOT NULL,
 discovery_path TEXT NOT NULL, discovery_url TEXT NOT NULL,
 repo_license TEXT NOT NULL DEFAULT '', repo_pushed_at TEXT,
 route_example TEXT NOT NULL, requires_auth INTEGER NOT NULL DEFAULT 0,
 requires_browser INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'discovered', reason TEXT NOT NULL DEFAULT '',
 observed_items INTEGER NOT NULL DEFAULT 0,
 last_checked TEXT, last_error TEXT, source_id INTEGER REFERENCES sources(id),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(discovery_repo, discovery_path, route_example)
);
CREATE INDEX IF NOT EXISTS idx_source_candidates_state
 ON source_candidates(state, updated_at);
"""


SEEDS = [
 ("什么值得买", "公开优惠线索", "零售优惠", "https://www.smzdm.com/", "html", "smzdm", 1),
 ("Apple 中国", "认证翻新产品", "新品与补货", "https://www.apple.com.cn/shop/refurbished", "html", "apple", 1),
 ("Apple 中国", "Mac 认证翻新子栏目", "新品与补货", "https://www.apple.com.cn/shop/refurbished/mac", "html", "apple", 0),
 ("Apple 中国", "iPad 认证翻新子栏目", "新品与补货", "https://www.apple.com.cn/shop/refurbished/ipad", "html", "apple", 0),
 ("小米商城", "公开商品入口", "新品与补货", "https://www.mi.com/shop", "html", "mi", 1),
 ("中国政府采购网", "中央公告", "服务与合作", "https://www.ccgp.gov.cn/cggg/zygg/", "html", "ccgp", 1),
 ("中国政府采购网", "中央公开招标子栏目", "服务与合作", "https://www.ccgp.gov.cn/cggg/zygg/gkzb/", "html", "ccgp", 0),
 ("中国政府采购网", "地方公开招标子栏目", "服务与合作", "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/", "html", "ccgp", 1),
 ("义乌购", "库存尾货候选入口", "供货与清仓", "https://www.yiwugo.com/buy/list/1.html", "html", "yiwugo", 0),
 ("孔夫子旧书网", "公开在售图书入口", "二手与闲置", "https://book.kongfz.com/", "html", "kongfz", 1),
 ("苏宁易购", "公开商品入口", "零售优惠", "https://www.suning.com/", "html", "suning", 1),
 ("联想商城", "官方商品入口", "新品与补货", "https://www.lenovo.com.cn/", "html", "lenovo", 1),
 ("荣耀商城", "官方商品入口", "新品与补货", "https://www.honor.com/cn/shop/", "html", "honor", 1),
 ("闲鱼", "人工线索", "二手与闲置", "https://www.goofish.com/", "manual", "", 0),
 ("京东拍卖", "司法拍卖候选", "拍卖与资产", "https://auction.jd.com/sifa.html", "manual", "", 0),
 ("小米众筹", "RSSHub 公开众筹新品", "新品与补货", "http://127.0.0.1:1200/mi/crowdfunding", "rsshub", "", 1),
 ("宜家中国", "RSSHub 低价优选", "零售优惠", "http://127.0.0.1:1200/ikea/cn/low_price", "rsshub", "", 1),
 ("酷比科技", "RSSHub 最新商品", "新品与补货", "http://127.0.0.1:1200/coolbuy/newest", "rsshub", "", 1),
 ("麦当劳中国", "RSSHub 公开活动资讯", "零售优惠", "http://127.0.0.1:1200/mcdonalds/cn/sales+event", "rsshub", "", 1),
]


CATEGORY_SEEDS = [
    ("日用百货", "叶子类另配", "品牌/型号、用途、地区及包装规格匹配", "home", []),
    ("纸品", "元/100抽或元/卷；仅在抽数/卷数明确时换算", "品牌、层数、单包规格、包数一致；抽纸与卷纸分开", "home", ["抽纸", "卷纸", "纸巾", "面巾纸", "卫生纸", "厨房纸", "湿厕纸", "湿巾"]),
    ("清洁洗护", "元/升或元/千克；浓缩倍率独立", "品牌、品名、净含量、浓度/型号一致", "home", ["洗衣液", "洗衣凝珠", "洗洁精", "垃圾袋", "清洁剂", "消毒液"]),
    ("乳品饮料", "元/升或元/100克；箱规明确", "品牌、口味、净含量、件数、保质状态一致", "food", ["牛奶", "酸奶", "奶酪", "饮料", "咖啡", "茶饮", "矿泉水"]),
    ("食品", "元/千克或元/100克；按可食净含量", "品牌、品类、净含量、等级及保质状态一致", "food", ["零食", "坚果", "粮油", "米面", "调味", "水果", "生鲜", "午餐肉"]),
    ("手机数码", "元/件；不做跨型号单位折算", "精确型号、容量、版本、成色、保修、地区一致", "electronics", ["手机", "iPhone", "iPad", "REDMI", "电脑", "笔记本", "显示器", "耳机"]),
    ("家电", "元/件；型号一致", "精确型号、地区、安装服务、保修与新旧状态一致", "electronics", ["冰箱", "洗衣机", "空调", "电饭煲", "吸尘器"]),
    ("本地生活", "元/次或元/份；需拆资格和门店", "城市/门店、时段、规格、会员及新客资格一致", "travel", ["外卖", "餐券", "酒店", "机票", "打车", "电影", "加油"]),
    ("二手与收藏", "元/件；不混合新品价格", "型号/版本、品相、附件、真伪证据及交易保障一致", "other", ["二手", "闲置", "收藏", "拍卖", "古董"]),
]


def initialize_benchmark_rules(db):
    """Add/seed only benchmark configuration, for safe targeted migration and app init."""
    db.execute("""CREATE TABLE IF NOT EXISTS benchmark_category_rule_audit (
        id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, version INTEGER NOT NULL,
        previous_policy_json TEXT, policy_json TEXT NOT NULL,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS benchmark_topic_rules (
        topic_key TEXT PRIMARY KEY, policy_json TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS benchmark_topic_rule_audit (
        id INTEGER PRIMARY KEY, topic_key TEXT NOT NULL, version INTEGER NOT NULL,
        previous_policy_json TEXT, policy_json TEXT NOT NULL,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    category_columns = {row[1] for row in db.execute("PRAGMA table_info(benchmark_categories)")}
    for column, declaration in (
        ("topic_key", "TEXT NOT NULL DEFAULT 'other'"),
        ("match_terms_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("policy_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("comparison_rule_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if column not in category_columns:
            db.execute(f"ALTER TABLE benchmark_categories ADD COLUMN {column} {declaration}")
    category_policy_json = encode_policy(default_policy())
    category_comparison_rule_json = encode_rule(default_rule())
    for name, unit, identity, topic_key, terms in CATEGORY_SEEDS:
        db.execute("""INSERT OR IGNORE INTO benchmark_categories
            (name,pricing_unit,identity_rule,topic_key,match_terms_json,policy_json,comparison_rule_json)
            VALUES(?,?,?,?,?,?,?)""",
            (name, unit, identity, topic_key, json.dumps(terms,ensure_ascii=False),category_policy_json,
             encode_rule(default_rule())))
        # Upgrade only untouched legacy seeds; preserve every user-edited rule.
        db.execute("""UPDATE benchmark_categories SET topic_key=?,match_terms_json=?,policy_json=?,comparison_rule_json=?
            WHERE name=? AND topic_key='other' AND match_terms_json='[]' AND policy_json='{}' AND version=1""",
            (topic_key,json.dumps(terms,ensure_ascii=False),category_policy_json,encode_rule(default_rule()),name))
    # Existing rows receive the new conservative default exactly once; preserve
    # every non-empty user-configured comparison rule.
    db.execute("""UPDATE benchmark_categories SET comparison_rule_json=?
        WHERE comparison_rule_json='{}'""", (category_comparison_rule_json,))
    policy_json = encode_policy(default_policy())
    for topic_key in TOPIC_LABELS:
        db.execute("INSERT OR IGNORE INTO benchmark_topic_rules(topic_key,policy_json) VALUES(?,?)",
                   (topic_key, policy_json))


def initialize():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        initialize_benchmark_rules(db)
        provider_seeds = [
            ("smzdm_history", "什么值得买历史价格 API", "independent_index", "按商品URL的历史价格曲线；Price/FinalPrice字段", "需App Key与OAuth；历史索引不等于当前结算价", "https://openapi.zhidemai.com/pages/price/4.%E5%8E%86%E5%8F%B2%E4%BB%B7%E6%A0%BC%E6%9F%A5%E8%AF%A2API.html"),
            ("manmanbuy", "慢慢买", "independent_index", "历史价格与跨商城比价；当前可用 API 覆盖待合作核实", "公开介绍未提供本项目可直接调用的接口契约，需合作询价", "https://help.manmanbuy.com/"),
            ("taobao_union", "淘宝联盟商品推广 API", "affiliate", "联盟推广商品与促销字段", "需AppKey/推广位；仅推广集合，非淘宝全站中立总体", "https://developer.alibaba.com/docs/api.htm?apiId=69450"),
            ("jd_iop", "京东 IOP 售卖价", "authorized_platform", "授权客户商品池 SKU 价格", "需 token 与授权；不是无条件全站查询", "https://opendoc.jd.com/iopv2/iopv2/%E4%BB%B7%E6%A0%BC/%E6%9F%A5%E8%AF%A2%E5%95%86%E5%93%81%E5%94%AE%E5%8D%96%E4%BB%B7.html"),
            ("douyin_local", "抖音生活服务商品查询", "authorized_platform", "授权商户的本地生活商品与有限价格字段", "需权限申请和商户授权；不是抖音全站商品价", "https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/online.get"),
            ("cneptp", "全国企业采购交易寻源询价平台", "procurement_index", "采购品类/地区/周期参考价与历史", "需 accessToken；仅其企业采购覆盖，不能外推家用零售价", "https://apidoc.cneptp.com/price-track/bp/average-price-search.html"),
            ("github_price_tracker", "GitHub price-tracker", "implementation_reference", "借鉴按商品保留多卖家历史序列", "开源实现参考，不提供中国商城数据", "https://github.com/andrewschultzw/price-tracker"),
            ("github_price_scout", "GitHub price-scout", "implementation_reference", "借鉴商品分组、单位归一和购物篮比较", "开源实现参考，不提供中国商城数据", "https://github.com/bulletinmybeard/price-scout"),
        ]
        for key, name, kind, scope, note, url in provider_seeds:
            db.execute("""INSERT OR IGNORE INTO benchmark_providers
                (provider_key,name,kind,scope,note,evidence_url,access_state,sample_count)
                VALUES(?,?,?,?,?,?,'not_connected',0)""", (key,name,kind,scope,note,url))
        trade_columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
        if "deposit_lost_cents" not in trade_columns:
            db.execute("ALTER TABLE trades ADD COLUMN deposit_lost_cents INTEGER NOT NULL DEFAULT 0")
        opportunity_columns = {row[1] for row in db.execute("PRAGMA table_info(opportunities)")}
        for column,default in [('resource_kind','unknown'),('topic','other')]:
            if column not in opportunity_columns:
                db.execute(f"ALTER TABLE opportunities ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        if "buy_checked_at" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN buy_checked_at TEXT")
        if "buy_proof" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN buy_proof TEXT NOT NULL DEFAULT ''")
        if "offer_type" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN offer_type TEXT NOT NULL DEFAULT 'unknown'")
        if "eligibility" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN eligibility TEXT NOT NULL DEFAULT 'unknown'")
        if "purchase_limit" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN purchase_limit INTEGER")
        for row in db.execute("SELECT id,title FROM opportunities WHERE offer_type='unknown'").fetchall():
            if detected_offer_type(row["title"]) == "suspected_new_user":
                db.execute("UPDATE opportunities SET offer_type='suspected_new_user' WHERE id=?", (row["id"],))
        quote_columns = {row[1] for row in db.execute("PRAGMA table_info(quotes)")}
        event_columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        benefit_columns = {row[1] for row in db.execute("PRAGMA table_info(benefit_resources)")}
        if "state" not in benefit_columns:
            db.execute("ALTER TABLE benefit_resources ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
        if "reason" not in benefit_columns:
            db.execute("ALTER TABLE benefit_resources ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        db.execute("""INSERT OR IGNORE INTO benefit_resource_sources
            (resource_id,source_key,source_type,source_url,source_opportunity_id,
             source_title,origin_text,published_at,first_seen_at,last_seen_at)
            SELECT id,
              CASE WHEN source_opportunity_id IS NOT NULL THEN 'opportunity:'||source_opportunity_id
                   ELSE source_type||':'||COALESCE(NULLIF(source_url,''),url) END,
              source_type,source_url,source_opportunity_id,title,origin_text,published_at,
              first_seen_at,last_seen_at FROM benefit_resources""")
        db.execute("""INSERT OR IGNORE INTO benefit_product_relations
            (resource_id,opportunity_id,state,basis,evidence_url)
            SELECT resource_id,source_opportunity_id,'source_linked',
              '优惠入口与商品出现在同一条来源记录中',source_url
            FROM benefit_resource_sources WHERE source_opportunity_id IS NOT NULL""")
        if 'metadata_json' not in event_columns:
            db.execute("ALTER TABLE events ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        if "last_seen_at" not in event_columns:
            db.execute("ALTER TABLE events ADD COLUMN last_seen_at TEXT")
        db.execute("UPDATE events SET last_seen_at=observed_at WHERE last_seen_at IS NULL")
        if "price_at" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN price_at TEXT")
        if "valid_until" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN valid_until TEXT")
        if "final_quote" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN final_quote INTEGER NOT NULL DEFAULT 0")
        if "offer_type" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN offer_type TEXT NOT NULL DEFAULT 'unknown'")
        for platform, name, category, url, method, parser, enabled in SEEDS:
            db.execute("""INSERT OR IGNORE INTO sources
                (platform,name,category,url,method,parser,enabled)
                VALUES(?,?,?,?,?,?,?)""",
                (platform,name,category,url,method,parser,enabled))
