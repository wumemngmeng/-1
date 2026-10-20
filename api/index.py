"""Vercel Python Function adapter for the existing CRM handler."""
from web.server import CustomHandler


class handler(CustomHandler):
    """Concrete handler class detected by the Vercel Python runtime."""
