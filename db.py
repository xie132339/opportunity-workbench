"""SQLite storage for the local opportunity workbench."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

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
 snippet TEXT, fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 published_at TEXT, is_baseline INTEGER NOT NULL DEFAULT 0,
 UNIQUE(source_id, external_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS opportunities (
 id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE REFERENCES events(id),
 source_id INTEGER REFERENCES sources(id), title TEXT NOT NULL, category TEXT NOT NULL,
 url TEXT, notes TEXT NOT NULL DEFAULT '', specification TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', buy_cents INTEGER,
 buy_checked_at TEXT, buy_proof TEXT NOT NULL DEFAULT '',
 buy_shipping_cents INTEGER, sell_shipping_cents INTEGER,
 platform_fee_cents INTEGER, processing_cents INTEGER,
 other_cents INTEGER, reserve_cents INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotes (
 id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
 kind TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 specification TEXT NOT NULL DEFAULT '', conditions TEXT NOT NULL DEFAULT '',
 same_spec INTEGER NOT NULL DEFAULT 0, final_quote INTEGER NOT NULL DEFAULT 0,
 evidence_url TEXT, price_at TEXT, valid_until TEXT,
 observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
 ("闲鱼", "人工线索", "二手与闲置", "https://www.goofish.com/", "manual", "", 0),
 ("京东拍卖", "司法拍卖候选", "拍卖与资产", "https://auction.jd.com/sifa.html", "manual", "", 0),
]


def initialize():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        trade_columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
        if "deposit_lost_cents" not in trade_columns:
            db.execute("ALTER TABLE trades ADD COLUMN deposit_lost_cents INTEGER NOT NULL DEFAULT 0")
        opportunity_columns = {row[1] for row in db.execute("PRAGMA table_info(opportunities)")}
        if "buy_checked_at" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN buy_checked_at TEXT")
        if "buy_proof" not in opportunity_columns:
            db.execute("ALTER TABLE opportunities ADD COLUMN buy_proof TEXT NOT NULL DEFAULT ''")
        quote_columns = {row[1] for row in db.execute("PRAGMA table_info(quotes)")}
        if "price_at" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN price_at TEXT")
        if "valid_until" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN valid_until TEXT")
        if "final_quote" not in quote_columns:
            db.execute("ALTER TABLE quotes ADD COLUMN final_quote INTEGER NOT NULL DEFAULT 0")
        for platform, name, category, url, method, parser, enabled in SEEDS:
            db.execute("""INSERT OR IGNORE INTO sources
                (platform,name,category,url,method,parser,enabled)
                VALUES(?,?,?,?,?,?,?)""",
                (platform,name,category,url,method,parser,enabled))
