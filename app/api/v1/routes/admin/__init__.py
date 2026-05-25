from fastapi import APIRouter
from app.api.v1.routes.admin.base import configure as configure_base

# Import all sub-routers
from app.api.v1.routes.admin.auth import router as auth_router
from app.api.v1.routes.admin.dashboard import router as dashboard_router
from app.api.v1.routes.admin.settings import router as settings_router
from app.api.v1.routes.admin.security import router as security_router
from app.api.v1.routes.admin.users import router as users_router
from app.api.v1.routes.admin.devices import router as devices_router
from app.api.v1.routes.admin.branches import router as branches_router
from app.api.v1.routes.admin.employees import router as employees_router
from app.api.v1.routes.admin.companies import router as companies_router
from app.api.v1.routes.admin.groups import router as groups_router
from app.api.v1.routes.admin.shifts import router as shifts_router
from app.api.v1.routes.admin.holidays import router as holidays_router
from app.api.v1.routes.admin.leaves import router as leaves_router
from app.api.v1.routes.admin.audit import router as audit_router
from app.api.v1.routes.admin.reports import router as reports_router
from app.api.v1.routes.admin.errors import router as errors_router
from app.api.v1.routes.admin.roster import router as roster_router

# Combine routers
router = APIRouter()
router.include_router(auth_router)
router.include_router(dashboard_router)
router.include_router(settings_router)
router.include_router(security_router)
router.include_router(users_router)
router.include_router(devices_router)
router.include_router(branches_router)
router.include_router(employees_router)
router.include_router(companies_router)
router.include_router(groups_router)
router.include_router(shifts_router)
router.include_router(holidays_router)
router.include_router(leaves_router)
router.include_router(audit_router)
router.include_router(reports_router)
router.include_router(errors_router)
router.include_router(roster_router)

def configure(arq_pool_ref):
    """Propagate the global ARQ pool reference to base module."""
    configure_base(arq_pool_ref)
