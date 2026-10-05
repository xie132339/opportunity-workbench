"""Source configuration, discovery and scanning routes."""
import os
import requests
from urllib.parse import urlparse
from flask import flash, render_template, request
from db import connect
from channel_discovery import refresh as refresh_channel_candidates, validate_many as validate_channel_candidates, promote as promote_channel_candidate
from scanner import _local_adapter_url, _public_url, scan_all, scan_source
from xianbao import validate_url as validate_xianbao_url
from routes.common import go
from services.source_metrics import current_source_yield

def sources():
    with connect() as db:
        rows = db.execute("""SELECT s.*,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id) event_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id
                               AND s.enabled=1 AND s.status='healthy'
                               AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                               AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                               AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')) recent_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id AND e.published_at IS NULL) unknown_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id AND e.published_at>CURRENT_TIMESTAMP) future_count
                             FROM sources s ORDER BY s.enabled DESC,s.id""").fetchall()
        metrics = current_source_yield(db)
        candidates = db.execute("""SELECT * FROM source_candidates
            ORDER BY CASE state WHEN 'validated' THEN 0 WHEN 'discovered' THEN 1
                       WHEN 'failed' THEN 2 WHEN 'rejected' THEN 3 ELSE 4 END,
                     updated_at DESC,id DESC""").fetchall()
    return render_template("sources.html", rows=rows, candidates=candidates, metrics=metrics)

def source_candidates_refresh():
    try:
        result = refresh_channel_candidates()
        flash(f"GitHub 候选目录已刷新：检索 {result['matched_files']} 个路由文件，"
              f"记录 {result['discovered']} 个相关候选，其中 {result['rejected']} 个因账号或浏览器要求被隔离；"
              f"{result['fetch_errors']} 个源码文件本轮读取失败，可下次重试", "ok")
    except Exception as exc:
        flash("GitHub 候选刷新失败：" + str(exc)[:300], "error")
    return go("sources")

def source_candidates_bulk():
    action = request.form.get("action", "")
    raw_ids = request.form.getlist("candidate_ids")
    if action not in ("validate", "promote") or not raw_ids or len(raw_ids) > 50:
        flash("请选择 1 到 50 个候选渠道和有效操作", "error")
        return go("sources")
    try:
        if any(not value.isdecimal() or int(value) <= 0 for value in raw_ids):
            raise ValueError("候选渠道编号无效")
        ids = sorted(set(int(value) for value in raw_ids))
        if action == "validate":
            results = validate_channel_candidates(ids)
            passed = sum(result["state"] == "validated" for result in results)
            items = sum(result["items"] for result in results)
            flash(f"已实测 {len(results)} 个候选；通过 {passed} 个，共解析 {items} 条当前数据",
                  "ok" if passed == len(results) else "error")
        else:
            source_ids = [promote_channel_candidate(candidate_id) for candidate_id in ids]
            outcomes = [scan_source(source_id) for source_id in source_ids]
            healthy = sum(item["status"] == "healthy" for item in outcomes)
            flash(f"已接入 {len(source_ids)} 个渠道并立即采集；正常 {healthy} 个，"
                  f"新增 {sum(item['new'] for item in outcomes)} 条", "ok" if healthy == len(outcomes) else "error")
    except Exception as exc:
        flash(str(exc)[:300], "error")
    return go("sources")

def source_add():
    platform = request.form.get("platform", "").strip()
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    url = request.form.get("url", "").strip()
    method = request.form.get("method", "manual")
    parser = request.form.get("parser", "") if method == "html" else ""
    parser_hosts = {"smzdm": "www.smzdm.com", "apple": "www.apple.com.cn",
                    "mi": "www.mi.com", "ccgp": "www.ccgp.gov.cn",
                    "yiwugo": "www.yiwugo.com", "kongfz": "book.kongfz.com",
                    "suning": "www.suning.com", "lenovo": "www.lenovo.com.cn",
                    "honor": "www.honor.com"}
    if not all((platform, name, category)) or method not in ("manual", "monitor", "rss", "rsshub", "goofish", "html", "xianbao"):
        flash("来源信息不完整", "error")
        return go("sources")
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        minimum = 1 if method=='xianbao' else 5
        if not minimum <= interval <= 1440:
            raise ValueError(f"检查间隔须在 {minimum} 到 1440 分钟之间")
        if method == 'xianbao':
            validate_xianbao_url(url)
        elif method == "rsshub":
            _local_adapter_url(url, os.environ.get("RSSHUB_BASE", "http://127.0.0.1:1200"), "/")
        elif method == "goofish":
            _local_adapter_url(url, os.environ.get("GOOFISH_BASE", "http://127.0.0.1:8000"), "/api/results/")
            if not urlparse(url).path.endswith(".jsonl"):
                raise ValueError("闲鱼结果地址须以 .jsonl 结尾")
        else:
            _public_url(url)
        if method == "html" and (parser not in parser_hosts or urlparse(url).hostname != parser_hosts[parser]):
            raise ValueError("公开网页解析须选择与入口域名一致的已适配站点")
        with connect() as db:
            if db.execute("SELECT 1 FROM sources WHERE platform=? AND url=? AND method=?",
                          (platform,url,method)).fetchone():
                raise ValueError("该平台和入口已经添加")
        watch_uuid = None
        if method == "monitor":
            token = os.environ.get("CHANGED_API_TOKEN")
            if not token:
                raise ValueError("未配置监控 API 凭据")
            resp = requests.post("http://127.0.0.1:5001/api/v1/watch",
                                 headers={"x-api-key": token},
                                 json={"url": url, "title": name}, timeout=15)
            resp.raise_for_status()
            watch_uuid = resp.json()["uuid"]
        with connect() as db:
            db.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,watch_uuid,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (platform,name,category,url,method,parser,watch_uuid,"pending" if method != "manual" else "manual",
                 int(method != "manual"),interval))
        flash("来源已保存", "ok")
    except Exception as exc:
        flash("来源保存失败：" + str(exc)[:180], "error")
    return go("sources")

def source_toggle(source_id):
    with connect() as db:
        db.execute("""UPDATE sources SET enabled=1-enabled,
            status=CASE WHEN enabled=1 THEN 'paused' ELSE 'pending' END WHERE id=?""", (source_id,))
    return go("sources")

def source_bulk():
    action = request.form.get("action", "")
    raw_ids = request.form.getlist("source_ids")
    if action not in ("enable", "pause", "interval", "scan") or not raw_ids or len(raw_ids) > 50:
        flash("请选择 1 到 50 个渠道和有效的批量操作", "error")
        return go("sources")
    try:
        if any(not value.isdecimal() or int(value) <= 0 for value in raw_ids):
            raise ValueError("渠道编号无效")
        ids = sorted(set(int(value) for value in raw_ids))
        placeholders = ",".join("?" for _ in ids)
        with connect() as db:
            rows = db.execute(f"SELECT id,method,enabled FROM sources WHERE id IN ({placeholders})", ids).fetchall()
            if len(rows) != len(ids) or any(row["method"] == "manual" for row in rows):
                raise ValueError("所选渠道不存在或为人工来源，无法批量修改")
            if action == "scan":
                if any(not row["enabled"] for row in rows):
                    raise ValueError("请先启用所选渠道，再批量检查")
            elif action == "interval":
                interval = int(request.form.get("interval_minutes", ""))
                minimum = 1 if all(row['method']=='xianbao' for row in rows) else 5
                if not minimum <= interval <= 1440:
                    raise ValueError(f"检查间隔须在 {minimum} 到 1440 分钟之间")
                db.execute(f"UPDATE sources SET interval_minutes=? WHERE id IN ({placeholders})", [interval, *ids])
            elif action == "enable":
                db.execute(f"UPDATE sources SET enabled=1,status=CASE WHEN enabled=0 THEN 'pending' ELSE status END WHERE id IN ({placeholders})", ids)
            else:
                db.execute(f"UPDATE sources SET enabled=0,status='paused' WHERE id IN ({placeholders})", ids)
        if action == "scan":
            results = [scan_source(source_id) for source_id in ids]
            healthy = sum(result["status"] == "healthy" for result in results)
            flash(f"已检查 {len(results)} 个渠道；正常 {healthy} 个，新增 {sum(result['new'] for result in results)} 条" ,
                  "ok" if healthy == len(results) else "error")
        else:
            label = {"enable": "启用", "pause": "暂停", "interval": "更新频率"}[action]
            flash(f"已批量{label} {len(ids)} 个渠道", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("sources")

def source_interval(source_id):
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        with connect() as db:
            source = db.execute('SELECT method FROM sources WHERE id=?',(source_id,)).fetchone()
            if source is None:raise ValueError('来源不存在')
            minimum = 1 if source['method']=='xianbao' else 5
            if not minimum <= interval <= 1440:raise ValueError(f'检查间隔须在 {minimum} 到 1440 分钟之间')
            db.execute("UPDATE sources SET interval_minutes=? WHERE id=?", (interval,source_id))
        flash("检查间隔已更新", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("sources")

def scan_one(source_id):
    try:
        outcome = scan_source(source_id)
        flash(f"检查结果：{outcome['status']}；本次新增 {outcome['new']} 条" +
              ("；首次采集为基线" if outcome.get("baseline") else "") +
              ("；" + outcome["error"] if outcome.get("error") else ""),
              "ok" if outcome["status"] == "healthy" else "error")
    except Exception as exc:
        flash(str(exc), "error")
    return go("sources")

def scan_everything():
    results = scan_all()
    flash(f"检查 {len(results)} 个来源；成功 {sum(x['status']=='healthy' for x in results)} 个；新增 {sum(x['new'] for x in results)} 条", "ok")
    return go("sources")

def register(app):
    app.add_url_rule('/sources', endpoint='sources', view_func=sources, methods=['GET'])
    app.add_url_rule('/sources/candidates/refresh', endpoint='source_candidates_refresh', view_func=source_candidates_refresh, methods=['POST'])
    app.add_url_rule('/sources/candidates/bulk', endpoint='source_candidates_bulk', view_func=source_candidates_bulk, methods=['POST'])
    app.add_url_rule('/sources', endpoint='source_add', view_func=source_add, methods=['POST'])
    app.add_url_rule('/sources/<int:source_id>/toggle', endpoint='source_toggle', view_func=source_toggle, methods=['POST'])
    app.add_url_rule('/sources/bulk', endpoint='source_bulk', view_func=source_bulk, methods=['POST'])
    app.add_url_rule('/sources/<int:source_id>/interval', endpoint='source_interval', view_func=source_interval, methods=['POST'])
    app.add_url_rule('/sources/<int:source_id>/scan', endpoint='scan_one', view_func=scan_one, methods=['POST'])
    app.add_url_rule('/sources/scan-all', endpoint='scan_everything', view_func=scan_everything, methods=['POST'])
