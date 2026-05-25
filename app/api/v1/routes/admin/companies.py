from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import Company, Branch, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import CompanyCreate, CompanyUpdate, CompanyResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Company Management"])

@router.get("/ui/companies", response_model=list[CompanyResponse])
async def get_companies(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(Company).order_by(Company.name).all()

@router.post("/ui/companies", response_model=CompanyResponse)
async def create_company(
    payload: CompanyCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    existing = db.query(Company).filter(Company.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Company code already exists.")
    
    company = Company(
        name=payload.name,
        code=payload.code,
        is_active=payload.is_active,
        shift_schedule_id=payload.shift_schedule_id
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    
    log_audit_action(db, admin.username, "created_company", "company", company.id, f"Created company {company.name} ({company.code})", request)
    return company

@router.put("/ui/companies/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
        
    if payload.name is not None:
        company.name = payload.name
    if payload.code is not None:
        existing = db.query(Company).filter(Company.code == payload.code, Company.id != company_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Company code already exists.")
        company.code = payload.code
    if payload.is_active is not None:
        company.is_active = payload.is_active
    if payload.shift_schedule_id is not None:
        company.shift_schedule_id = payload.shift_schedule_id
        
    company.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(company)
    
    log_audit_action(db, admin.username, "updated_company", "company", company.id, f"Updated company {company.name}", request)
    return company

@router.delete("/ui/companies/{company_id}")
async def delete_company(
    company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    has_branches = db.query(Branch).filter(Branch.company_id == company_id).first()
    if has_branches:
        raise HTTPException(status_code=400, detail="Cannot delete company. It still has active branches associated.")
        
    company_name = company.name
    db.delete(company)
    db.commit()
    
    log_audit_action(db, admin.username, "deleted_company", "company", company_id, f"Deleted company {company_name}", request)
    return {"status": "success", "message": "Company deleted successfully."}

@router.get("/ui/companies/{company_id}/branches")
async def get_company_branches(
    company_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Fetch branches belonging to a specific company for cascading selectors."""
    branches = db.query(Branch).filter(Branch.company_id == company_id, Branch.is_active == True).order_by(Branch.name).all()
    return [{"id": b.id, "name": b.name} for b in branches]
