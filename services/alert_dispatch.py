"""Send-time revalidation and delivery of eligible opportunity alerts."""
from db import connect
from notifier import active_channels, deliver
from services.money import money
from services.opportunity_analysis import estimated_profit, is_evidence_candidate, resale_assessment

def dispatch_verified_alerts():
    """Fan out only evidence-backed candidates; failed sends require manual retry."""
    channels = active_channels()
    if not channels:
        return {"eligible": 0, "sent": 0, "failed": 0}
    eligible = 0
    with connect() as db:
        rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
        rows = db.execute("""SELECT o.*,e.id AS source_event_id,e.published_at,e.last_seen_at,
                             s.method AS source_method,s.status AS source_status,
                             s.enabled AS source_enabled,s.interval_minutes AS source_interval,
                             s.last_success AS source_last_success,a.state AS auto_state
                             FROM opportunities o JOIN events e ON e.id=o.event_id
                             JOIN sources s ON s.id=o.source_id
                             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE o.status='verified'""").fetchall()
        for opp in rows:
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (opp["id"],)).fetchall()
            if not is_evidence_candidate(opp, quotes, rules):
                continue
            eligible += 1
            db.execute("""INSERT OR IGNORE INTO notifications (event_id,opportunity_id)
                          VALUES(?,?)""", (opp["source_event_id"], opp["id"]))
            notice_id = db.execute("SELECT id FROM notifications WHERE event_id=?",
                                   (opp["source_event_id"],)).fetchone()[0]
            for channel in channels:
                db.execute("""INSERT OR IGNORE INTO notification_deliveries
                              (notification_id,channel) VALUES(?,?)""", (notice_id, channel))
        tasks = db.execute("""SELECT d.id,d.channel,o.id AS opportunity_id,o.title,o.url
                              FROM notification_deliveries d
                              JOIN notifications n ON n.id=d.notification_id
                              JOIN opportunities o ON o.id=n.opportunity_id
                              WHERE d.status='pending' ORDER BY d.id LIMIT 20""").fetchall()
    sent = failed = 0
    for task in tasks:
        with connect() as db:
            opp = db.execute("""SELECT o.*,e.published_at,e.last_seen_at,
                s.method AS source_method,s.status AS source_status,s.enabled AS source_enabled,
                s.interval_minutes AS source_interval,s.last_success AS source_last_success,a.state AS auto_state
                FROM opportunities o JOIN events e ON e.id=o.event_id
                JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                WHERE o.id=?""", (task["opportunity_id"],)).fetchone()
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (task["opportunity_id"],)).fetchall()
        profit, _ = estimated_profit(opp, quotes)
        allowed, gate_basis = resale_assessment(opp, quotes)
        if not allowed or task["channel"] not in active_channels():
            with connect() as db:
                db.execute("UPDATE notification_deliveries SET status='skipped',last_error='条件已变化或通道已关闭' WHERE id=?",
                           (task["id"],))
            continue
        title = "转售测算达标线索：" + task["title"][:80]
        body = f"{gate_basis}\n保守价差 {money(profit)}；不代表已成交或已到账。\n原始链接：{task['url'] or '无'}"
        try:
            deliver(task["channel"], title, body)
            with connect() as db:
                db.execute("""UPDATE notification_deliveries SET status='sent',attempts=attempts+1,
                              sent_at=CURRENT_TIMESTAMP,last_error=NULL WHERE id=?""", (task["id"],))
            sent += 1
        except Exception as exc:
            with connect() as db:
                db.execute("""UPDATE notification_deliveries SET status='failed',attempts=attempts+1,
                              last_error=? WHERE id=?""", (str(exc)[:180], task["id"]))
            failed += 1
    return {"eligible": eligible, "sent": sent, "failed": failed}
