"""OFFER 两段式预取防回归：events 方向新鲜度过滤与完整 WHERE 语义等价。"""
import unittest
import sqlite3

import db as dbm
from routes.search import FRESHNESS_SQL, fresh_offer_event_ids, fresh_opportunity_counts


def build_fixture(db):
    cur = db.cursor()
    cur.execute(
        "INSERT INTO sources (id, platform, name, category, url, method, parser,"
        " status, enabled, interval_minutes, last_success, created_at)"
        " VALUES (1,'taobao','联盟源','c','http://src','taobao_alliance','taobao_alliance',"
        " 'healthy',1,30,datetime('now'),'2026-01-01')")
    for i in range(1, 5):
        cur.execute(
            "INSERT INTO events (id, source_id, external_key, title, url, fingerprint,"
            " observed_at, published_at, last_seen_at)"
            " VALUES (?,1,?,?, 'http://e', ?, datetime('now'),"
            " datetime('now','-30 minutes'), datetime('now','-1 minutes'))",
            (i, 'taobao:%d' % i, '商品%d' % i, 'fp%d' % i))
        cur.execute(
            "INSERT INTO opportunities (id, event_id, source_id, title, category,"
            " status, resource_kind, offer_type, created_at)"
            " VALUES (?,?,1,?,?, 'active','purchase','unknown',datetime('now'))",
            (i, i, '商品%d' % i, '零售优惠'))
        cur.execute(
            "INSERT INTO auto_reviews (opportunity_id, state, reason, specification,"
            " conditions, evidence, advertised_cents, checked_at)"
            " VALUES (?,'queued','r','','','',?,datetime('now'))",
            (i, 1000 + i))
    # 一条过期的（last_seen 旧），不应被两段式/完整 WHERE 命中
    cur.execute(
        "INSERT INTO events (id, source_id, external_key, title, url, fingerprint,"
        " observed_at, published_at, last_seen_at)"
        " VALUES (9,1,'taobao:9','旧商品','http://e','fp9',datetime('now'),"
        " datetime('now','-30 minutes'), datetime('now','-5 hours'))")
    cur.execute(
        "INSERT INTO opportunities (id, event_id, source_id, title, category,"
        " status, resource_kind, offer_type, created_at)"
        " VALUES (9,9,1,'旧商品','零售优惠','active','purchase','unknown',datetime('now'))")
    cur.execute(
        "INSERT INTO auto_reviews (opportunity_id, state, reason, specification,"
        " conditions, evidence, advertised_cents, checked_at)"
        " VALUES (9,'queued','r','','','',2000,datetime('now'))")
    db.commit()


SCOPE = (
    "FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id"
    " LEFT JOIN events e ON e.id=o.event_id"
    " LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE 1=1"
    " AND o.status!='ignored' AND (s.id IS NULL OR s.enabled=1 OR s.method='manual')"
    " AND o.resource_kind IN ('purchase','unknown')"
    " AND COALESCE(a.state,'queued') NOT IN ('stale','source_unavailable','retry','excluded')"
    + FRESHNESS_SQL +
    " AND s.method='taobao_alliance'"
    " AND o.source_id IN (SELECT id FROM sources WHERE method='taobao_alliance'"
    " AND enabled=1 AND status='healthy'"
    " AND last_success>=datetime('now','-' || MIN(2*interval_minutes+15,120) || ' minutes'))"
)


class FreshnessTwoStageTests(unittest.TestCase):
    def test_fresh_opportunity_counts_crosses_event_batch_boundary(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(dbm.SCHEMA)
        build_fixture(db)
        db.executemany(
            "INSERT INTO events (id,source_id,external_key,title,url,fingerprint,"
            " observed_at,published_at,last_seen_at) VALUES (?,1,?,?,?, ?,"
            " datetime('now'),datetime('now'),datetime('now'))",
            [(i, f'taobao:{i}', f'商品{i}', 'http://e', f'fp{i}') for i in range(100, 1003)],
        )
        db.execute("""INSERT INTO opportunities(id,event_id,source_id,title,category,status,
            resource_kind,offer_type,created_at)
            SELECT id,id,1,title,'零售优惠',CASE WHEN id=1001 THEN 'ignored' ELSE 'active' END,
                'purchase','unknown',datetime('now') FROM events WHERE id>=100""")
        db.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason)
            VALUES (1002,'stale','测试排除')""")
        counts = fresh_opportunity_counts(
            db, ["(e.source_id=1 AND e.last_seen_at>=datetime('now','-75 minutes'))"])
        self.assertEqual(counts, {1: 905})  # 4 original + 901 eligible; two excluded

    def test_two_stage_matches_full_where(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(dbm.SCHEMA)
        build_fixture(db)
        # 完整 WHERE（老逻辑）
        full = set(
            row['id'] for row in db.execute(
                "SELECT o.id, MAX(COALESCE(a.advertised_cents,0)) AS adv "
                + SCOPE + " GROUP BY o.id"))
        # 两段式（events 方向 + 分块 IN）
        scope_wo = SCOPE.replace(FRESHNESS_SQL, "")
        ids = [row['id'] for row in fresh_offer_event_ids(db)]
        self.assertEqual(ids, sorted(set(ids)))
        two_stage = set()
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ",".join("?" for _ in chunk)
            for row in db.execute(
                    "SELECT o.id, MAX(COALESCE(a.advertised_cents,0)) AS adv "
                    + scope_wo + " AND o.event_id IN (" + ph + ") GROUP BY o.id",
                    chunk):
                two_stage.add(row['id'])
        self.assertEqual(full, two_stage)
        self.assertIn(1, two_stage)   # 新鲜行命中
        self.assertNotIn(9, two_stage)  # 过期行被过滤

    def test_freshness_clause_constant_is_referenced(self):
        import routes.search as mod
        src = open(mod.__file__, encoding='utf-8').read()
        self.assertIn('sql += FRESHNESS_SQL', src)


if __name__ == '__main__':
    unittest.main()
