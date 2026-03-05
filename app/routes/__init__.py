from fastapi import APIRouter

from .auth import router as auth_router
from .admin import router as admin_router
from .admin_ewc import router as admin_ewc_router
from .customers import router as customers_router
from .admin_printing import router as admin_printing_router
from .admin_company import router as admin_company_router
from .invoices import router as invoices_router
from .products import router as products_router
from .setup import router as setup_router
from .superadmin import router as superadmin_router
from .tickets import router as tickets_router
from .vehicles import router as vehicles_router

api_router = APIRouter()
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(setup_router, tags=["setup"])
api_router.include_router(admin_router, tags=["admin"])
api_router.include_router(admin_ewc_router, tags=["admin-ewc"])
api_router.include_router(admin_printing_router, tags=["admin-printing"])
api_router.include_router(admin_company_router, tags=["admin-company"])
api_router.include_router(customers_router, tags=["customers"])
api_router.include_router(invoices_router, tags=["invoices"])
api_router.include_router(products_router, tags=["products"])
api_router.include_router(tickets_router, tags=["tickets"])
api_router.include_router(vehicles_router, tags=["vehicles"])
api_router.include_router(superadmin_router, tags=["superadmin"])
