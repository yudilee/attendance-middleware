"""
Admin UI routing entry point.

Exposes a thin re-export wrapper of the modular app.api.v1.routes.admin package
to maintain 100% backward compatibility with main FastAPI application.
"""
from app.api.v1.routes.admin import router, configure
