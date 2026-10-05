"""Xianyu account, task and result-import routes."""
from flask import abort, flash, render_template, request
from db import connect
from scanner import scan_source
from xianyu import XianyuError, api as xianyu_api, create_task as create_xianyu_task
from xianyu import import_account as import_xianyu_account, result_url, snapshot as xianyu_snapshot
from xianyu import update_task as update_xianyu_task
from routes.common import go

def xianyu():
    accounts, tasks, files, error = [], [], [], None
    try:
        accounts, tasks, files = xianyu_snapshot()
    except XianyuError as exc:
        error = str(exc)
    with connect() as db:
        linked = {row[0] for row in db.execute("SELECT url FROM sources WHERE method='goofish'")}
    return render_template("xianyu.html", accounts=accounts, tasks=tasks,
                           files=files, linked=linked, error=error, result_url=result_url)

def xianyu_account():
    try:
        if request.content_length and request.content_length > 600_000:
            abort(413)
        import_xianyu_account(request.form.get("name", "").strip(),
                              request.form.get("content", ""))
        flash("闲鱼登录态已保存到本机采集服务；页面不会回显内容", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")

def xianyu_task_add():
    try:
        create_xianyu_task(request.form)
        flash("闲鱼搜索任务已创建；手动启动后才会访问闲鱼", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")

def xianyu_task_action(task_id, action):
    if action not in ("start", "stop", "update"):
        abort(404)
    try:
        tasks = xianyu_api("GET", "/api/tasks")
        task = next((item for item in tasks if item.get("id") == task_id), None)
        if not task:
            raise XianyuError("任务不存在")
        if action == "update":
            update_xianyu_task(task_id, task, request.form)
            flash("任务条件已保存", "ok")
            return go("xianyu")
        if action == "start":
            if not task.get("enabled"):
                raise XianyuError("任务已暂停，请先编辑条件并启用")
            account_file = task.get("account_state_file")
            accounts = xianyu_api("GET", "/api/accounts")
            if not account_file or not any(item.get("path") == account_file for item in accounts):
                raise XianyuError("任务没有可用的本机闲鱼账号，请先导入登录态")
        xianyu_api("POST", f"/api/tasks/{action}/{task_id}")
        flash("任务已启动，结果出现后可在本页接入工作台" if action == "start" else "任务已停止", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")

def xianyu_result_add():
    filename = request.form.get("filename", "")
    try:
        files = xianyu_api("GET", "/api/results/files").get("files", [])
        if filename not in files:
            raise XianyuError("该闲鱼结果尚未产生")
        url = result_url(filename)
        with connect() as db:
            db.execute("""INSERT OR IGNORE INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                ("闲鱼", "搜索结果 · " + filename.removesuffix("_full_data.jsonl"),
                 "二手与闲置", url, "goofish", "", "pending", 1, 60))
            source_id = db.execute("SELECT id FROM sources WHERE platform='闲鱼' AND url=? AND method='goofish'",
                                   (url,)).fetchone()[0]
        outcome = scan_source(source_id)
        if outcome["status"] != "healthy":
            raise XianyuError("结果来源已登记，但读取失败：" + outcome.get("error", "未知错误"))
        flash(f"闲鱼结果已接入工作台，本次读取 {outcome['seen']} 条；挂牌价仍待核实", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")

def register(app):
    app.add_url_rule('/xianyu', endpoint='xianyu', view_func=xianyu, methods=['GET'])
    app.add_url_rule('/xianyu/accounts', endpoint='xianyu_account', view_func=xianyu_account, methods=['POST'])
    app.add_url_rule('/xianyu/tasks', endpoint='xianyu_task_add', view_func=xianyu_task_add, methods=['POST'])
    app.add_url_rule('/xianyu/tasks/<int:task_id>/<action>', endpoint='xianyu_task_action', view_func=xianyu_task_action, methods=['POST'])
    app.add_url_rule('/xianyu/results', endpoint='xianyu_result_add', view_func=xianyu_result_add, methods=['POST'])
