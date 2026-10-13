"""Local sign-in and sign-out routes."""
import os

from flask import flash, redirect, render_template, request, session

from services.local_auth import safe_next_path, sign_in, sign_out, verify


def login():
    next_path = safe_next_path(request.values.get("next"))
    if request.method == "POST":
        if verify(request.form.get("username"), request.form.get("password"), os.environ):
            # sign_out 会清空整个 session（含 csrf）：登录成功后保留原 csrf，
            # 否则用户在登录页的重复提交（双击/重放）会因 token 缺失误报"会话过期"。
            csrf = session.get("csrf")
            sign_out(session)
            sign_in(session, request.form.get("username", ""))
            if csrf:
                session["csrf"] = csrf
            session.permanent = bool(request.form.get("remember"))
            return redirect(next_path)
        flash("账号或密码不正确。", "error")
        return render_template("login.html", next_path=next_path), 401
    return render_template("login.html", next_path=next_path)


def logout():
    sign_out(session)
    return redirect("/login")


def register(app):
    app.add_url_rule("/login", endpoint="login", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/logout", endpoint="logout", view_func=logout, methods=["POST"])
