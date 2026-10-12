"""Local sign-in and sign-out routes."""
import os

from flask import flash, redirect, render_template, request, session

from services.local_auth import safe_next_path, sign_in, sign_out, verify


def login():
    next_path = safe_next_path(request.values.get("next"))
    if request.method == "POST":
        if verify(request.form.get("username"), request.form.get("password"), os.environ):
            sign_out(session)
            sign_in(session, request.form.get("username", ""))
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
