"""Production WSGI entry point for the opportunity workbench."""
from app import app
from db import initialize

initialize()
