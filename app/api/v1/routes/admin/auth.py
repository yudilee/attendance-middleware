from fastapi import APIRouter, Depends, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database.models import AdminUser
from app.services.auth_ui import verify_password, create_access_token
from app.api.v1.routes.admin.base import templates, get_db

router = APIRouter(tags=["Admin UI - Auth"])

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})

@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": "Invalid username or password"}
        )

    access_token = create_access_token(data={"sub": user.username})
    redirect_response = RedirectResponse(url="/", status_code=302)
    redirect_response.set_cookie(
        key="dashboard_session", value=access_token,
        httponly=True, max_age=86400, samesite="lax",
    )
    return redirect_response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("dashboard_session")
    return response
