"""Shared HTTP helpers used by route modules."""
from flask import redirect, url_for

def go(endpoint, **kwargs):
    return redirect(url_for(endpoint, **kwargs))
