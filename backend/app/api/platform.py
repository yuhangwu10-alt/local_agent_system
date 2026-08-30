"""Platform authentication, wallet and model settings endpoints."""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import fitz

from app.api.deps import get_db
from app.models.platform import BillingQuote, LedgerEntry, ModelProfile, RedeemCode, User
from app.models.project import Project, SourceDocument
from app.utils.file_storage import get_input_dir, safe_join
from app.services.billing_service import count_billable_excel_rows, money
from app.services.auth_service import create_token, ensure_wallet, get_current_user, hash_password, require_admin, verify_password

router = APIRouter(prefix="/api", tags=["platform"])


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: str
    password: str


class RedeemRequest(BaseModel):
    code: str


class ModelProfileCreate(BaseModel):
    name: str
    provider: str
    base_url: str
    api_key: str = ""
    model: str
    stages: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(default=5, ge=1, le=100)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    retries: int = Field(default=2, ge=0, le=5)
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True
    is_default: bool = False


def _pdf_page_count(path):
    with fitz.open(str(path)) as pdf:
        return pdf.page_count


@router.post("/auth/register")
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    email = data.email.strip().lower()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="邮箱已注册")
    user = User(email=email, password_hash=hash_password(data.password), display_name=data.display_name.strip())
    db.add(user)
    await db.flush()
    await ensure_wallet(db, user.id)
    await db.commit()
    return {"token": create_token(user), "user": {"id": str(user.id), "email": user.email, "display_name": user.display_name, "role": user.role}}


@router.post("/auth/login")
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == data.email.strip().lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(data.password, user.password_hash) or not user.is_active:
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    await ensure_wallet(db, user.id)
    await db.commit()
    return {"token": create_token(user), "user": {"id": str(user.id), "email": user.email, "display_name": user.display_name, "role": user.role}}


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    wallet = await ensure_wallet(db, user.id)
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name, "role": user.role, "balance": wallet.balance, "frozen": wallet.frozen}


@router.post("/wallet/redeem")
async def redeem(data: RedeemRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    normalized = data.code.strip().upper()
    if len(normalized) < 8:
        raise HTTPException(status_code=400, detail="兑换码格式不正确")
    code_hash = hashlib.sha256(normalized.encode()).hexdigest()
    result = await db.execute(select(RedeemCode).where(RedeemCode.code_hash == code_hash).with_for_update())
    code = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if code is None or code.redeemed_by is not None or (code.expires_at and code.expires_at < now):
        raise HTTPException(status_code=400, detail="兑换码无效、已使用或已过期")
    wallet = await ensure_wallet(db, user.id)
    wallet.balance += code.amount
    code.redeemed_by = user.id
    code.redeemed_at = now
    db.add(LedgerEntry(user_id=user.id, amount=code.amount, balance_after=wallet.balance, entry_type="redeem", reference_id=str(code.id), description=f"兑换码 {code.code_hint}"))
    await db.commit()
    return {"balance": wallet.balance, "credited": code.amount}


@router.get("/wallet/ledger")
async def ledger(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    wallet = await ensure_wallet(db, user.id)
    result = await db.execute(select(LedgerEntry).where(LedgerEntry.user_id == user.id).order_by(LedgerEntry.created_at.desc()).limit(100))
    return {"balance": wallet.balance, "frozen": wallet.frozen, "entries": [{"id": str(row.id), "amount": row.amount, "balance_after": row.balance_after, "type": row.entry_type, "description": row.description, "created_at": row.created_at} for row in result.scalars()]}


@router.get("/admin/models")
async def list_models(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProfile).order_by(ModelProfile.priority, ModelProfile.name))
    return [{"id": str(row.id), "name": row.name, "provider": row.provider, "base_url": row.base_url, "model": row.model, "stages": row.stages or [], "max_concurrency": row.max_concurrency, "timeout_seconds": row.timeout_seconds, "retries": row.retries, "priority": row.priority, "enabled": row.enabled, "is_default": row.is_default, "api_key_set": bool(row.api_key)} for row in result.scalars()]


@router.post("/admin/models")
async def create_model(data: ModelProfileCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if data.is_default:
        await db.execute(update(ModelProfile).values(is_default=False))
    row = ModelProfile(**data.model_dump())
    db.add(row)
    await db.commit()
    return {"id": str(row.id), "name": row.name}


@router.patch("/admin/models/{model_id}")
async def update_model(model_id: uuid.UUID, data: ModelProfileCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = await db.get(ModelProfile, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if data.is_default:
        await db.execute(update(ModelProfile).values(is_default=False))
    values = data.model_dump()
    if not values["api_key"]:
        values["api_key"] = row.api_key
    for key, value in values.items():
        setattr(row, key, value)
    await db.commit()
    return {"id": str(row.id), "name": row.name}


@router.post("/admin/redeem-codes")
async def generate_codes(amount: float = 10, count: int = 1, batch_name: str | None = None, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if amount <= 0 or count < 1 or count > 1000:
        raise HTTPException(status_code=400, detail="面额或数量不合法")
    output = []
    for _index in range(count):
        plain = "FZ-" + secrets.token_urlsafe(12).upper().replace("-", "")[:16]
        db.add(RedeemCode(code_hash=hashlib.sha256(plain.encode()).hexdigest(), code_hint=plain[-4:], amount=amount, batch_name=batch_name))
        output.append(plain)
    await db.commit()
    return {"amount": amount, "count": count, "codes": output}


@router.get("/admin/users")
async def list_users(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    from app.models.platform import Wallet
    wallet_result = await db.execute(select(Wallet.user_id, Wallet.balance, Wallet.frozen))
    wallet_map = {user_id: {"balance": balance, "frozen": frozen} for user_id, balance, frozen in wallet_result.all()}
    return [{"id": str(row.id), "email": row.email, "display_name": row.display_name, "role": row.role,
             "is_active": row.is_active, "balance": (wallet_map.get(row.id) or {}).get("balance", 0),
             "frozen": (wallet_map.get(row.id) or {}).get("frozen", 0), "created_at": row.created_at}
            for row in users]


@router.patch("/admin/users/{user_id}")
async def update_user(user_id: uuid.UUID, payload: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row.id == admin.id and payload.get("is_active") is False:
        raise HTTPException(status_code=400, detail="不能禁用当前管理员账号")
    if "is_active" in payload:
        row.is_active = bool(payload["is_active"])
    if "role" in payload and payload["role"] in {"user", "admin"}:
        row.role = payload["role"]
    if "display_name" in payload and str(payload["display_name"]).strip():
        row.display_name = str(payload["display_name"]).strip()[:120]
    await db.commit()
    return {"id": str(row.id), "is_active": row.is_active, "role": row.role, "display_name": row.display_name}


@router.get("/admin/tasks")
async def list_all_tasks(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    from app.models.task import Task
    result = await db.execute(select(Task).order_by(Task.created_at.desc()).limit(200))
    return [{"id": str(row.id), "project_id": str(row.project_id), "user_id": str(row.user_id) if row.user_id else None,
             "task_type": row.task_type, "status": row.status, "progress": row.progress, "error": row.error,
             "charge_status": row.charge_status, "attempt_count": row.attempt_count, "created_at": row.created_at,
             "updated_at": row.updated_at} for row in result.scalars()]


@router.get("/admin/redeem-codes")
async def list_redeem_codes(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RedeemCode).order_by(RedeemCode.created_at.desc()).limit(500))
    return [{"id": str(row.id), "code_hint": row.code_hint, "amount": row.amount, "batch_name": row.batch_name,
             "redeemed": row.redeemed_by is not None, "redeemed_at": row.redeemed_at, "expires_at": row.expires_at,
             "created_at": row.created_at} for row in result.scalars()]

class QuoteRequest(BaseModel):
    project_id: uuid.UUID
    document_ids: list[uuid.UUID] = Field(default_factory=list)


@router.post("/billing/quote")
async def create_quote(data: QuoteRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(Project).where(Project.id == data.project_id)
    if user.role != "admin":
        query = query.where(Project.user_id == user.id)
    project = await db.scalar(query)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或无权访问")
    docs_query = select(SourceDocument).where(SourceDocument.project_id == project.id, SourceDocument.status != "deleted")
    if data.document_ids:
        docs_query = docs_query.where(SourceDocument.id.in_(data.document_ids))
    docs = (await db.execute(docs_query)).scalars().all()
    if not docs:
        raise HTTPException(status_code=400, detail="项目中没有可计费文档")
    if data.document_ids:
        found_ids = {doc.id for doc in docs}
        missing_ids = [str(doc_id) for doc_id in data.document_ids if doc_id not in found_ids]
        if missing_ids:
            raise HTTPException(status_code=400, detail="部分文档不存在、已删除或不属于当前项目")
    pdf_pages = 0
    for doc in docs:
        if doc.file_type.lower() != "pdf":
            continue
        if doc.total_pages:
            pdf_pages += int(doc.total_pages)
            continue
        try:
            pdf_pages += _pdf_page_count(safe_join(get_input_dir(project.id), doc.file_path))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无法读取 PDF 页数：{exc}") from exc
    excel_rows = 0
    for doc in docs:
        if doc.file_type.lower() not in {"xlsx", "excel"}:
            continue
        try:
            excel_path = safe_join(get_input_dir(project.id), doc.file_path)
            excel_rows += count_billable_excel_rows(excel_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无法读取 Excel 行数：{exc}") from exc
    if pdf_pages and excel_rows:
        raise HTTPException(status_code=400, detail="一次报价请只选择 PDF 或 Excel 文档")
    units, unit_type, price = (pdf_pages, "页", settings.default_page_price) if pdf_pages else (excel_rows, "条", settings.default_row_price)
    if units <= 0:
        raise HTTPException(status_code=400, detail="文档没有可计费内容")
    quote = BillingQuote(user_id=user.id, project_id=project.id,
                         document_ids=[str(doc.id) for doc in docs], units=units,
                         unit_type=unit_type, unit_price=price, total=money(units * price),
                         expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    db.add(quote)
    await db.commit()
    await db.refresh(quote)
    return {"id": str(quote.id), "units": quote.units, "unit_type": quote.unit_type, "unit_price": quote.unit_price, "total": quote.total, "status": quote.status, "document_ids": quote.document_ids, "expires_at": quote.expires_at}


@router.get("/billing/quotes")
async def list_quotes(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BillingQuote).where(BillingQuote.user_id == user.id).order_by(BillingQuote.created_at.desc()).limit(100))
    return [{"id": str(row.id), "project_id": str(row.project_id), "document_ids": row.document_ids or [], "units": row.units, "unit_type": row.unit_type, "total": row.total, "status": row.status, "expires_at": row.expires_at, "created_at": row.created_at} for row in result.scalars()]

