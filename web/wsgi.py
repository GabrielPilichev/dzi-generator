"""Production WSGI entry point: ``gunicorn --config deploy/gunicorn.conf.py``.

Validates the production environment before importing the application and fails
startup with a clear message instead of using development fallbacks.
"""

from web.production import create_production_app

app = create_production_app()
