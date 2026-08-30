import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models.platform import BillingQuote, LedgerEntry, Wallet


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _expired(quote: BillingQuote) -> bool:
    if quote.expires_at is None:
        return False
    expires_at = quote.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def count_billable_excel_rows(file_path: Path) -> int:
    """Count rows using the same rule for quotes and processing.

    A row is billable when it contains at least one non-empty business value.
    Header rows are represented by dataframe columns and are therefore not
    counted by pandas.read_excel.
    """
    sheets = pd.read_excel(file_path, sheet_name=None)
    return sum(int(len(frame.dropna(how="all"))) for frame in sheets.values())


async def reserve_quote(db: AsyncSession, quote_id: uuid.UUID, user_id: uuid.UUID) -> BillingQuote:
    result = await db.execute(select(BillingQuote).where(BillingQuote.id == quote_id, BillingQuote.user_id == user_id).with_for_update())
    quote = result.scalar_one_or_none()
    if quote is None:
        raise ValueError("报价不存在")
    if quote.status == "reserved":
        return quote
    if quote.status != "quoted":
        raise ValueError("报价已失效")
    if _expired(quote):
        quote.status = "expired"
        raise ValueError("报价已过期，请重新获取报价")
    wallet_result = await db.execute(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    wallet = wallet_result.scalar_one_or_none()
    if wallet is None or _as_decimal(wallet.balance) - _as_decimal(wallet.frozen) < _as_decimal(quote.total):
        raise ValueError("余额不足，请先兑换余额")
    wallet.frozen = money(_as_decimal(wallet.frozen) + _as_decimal(quote.total))
    quote.status = "reserved"
    return quote


async def settle_quote(db: AsyncSession, quote_id: uuid.UUID, user_id: uuid.UUID) -> BillingQuote:
    quote = await db.scalar(select(BillingQuote).where(BillingQuote.id == quote_id, BillingQuote.user_id == user_id).with_for_update())
    if quote is None:
        raise ValueError("报价不存在")
    if quote.status == "settled":
        return quote
    if quote.status != "reserved":
        raise ValueError("报价不是待结算状态")
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    wallet.frozen = money(max(Decimal("0"), _as_decimal(wallet.frozen) - _as_decimal(quote.total)))
    wallet.balance = money(_as_decimal(wallet.balance) - _as_decimal(quote.total))
    quote.status = "settled"
    db.add(LedgerEntry(user_id=user_id, amount=-quote.total, balance_after=wallet.balance, entry_type="usage", reference_id=str(quote.id), description=f"处理消耗 {quote.units:g}{quote.unit_type}"))
    return quote


async def refund_quote(db: AsyncSession, quote_id: uuid.UUID, user_id: uuid.UUID, reason: str = "任务失败退款") -> BillingQuote:
    quote = await db.scalar(select(BillingQuote).where(BillingQuote.id == quote_id, BillingQuote.user_id == user_id).with_for_update())
    if quote is None:
        raise ValueError("报价不存在")
    if quote.status in {"refunded", "quoted"}:
        return quote
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    if quote.status == "reserved":
        wallet.frozen = money(max(Decimal("0"), _as_decimal(wallet.frozen) - _as_decimal(quote.total)))
    elif quote.status == "settled":
        wallet.balance = money(_as_decimal(wallet.balance) + _as_decimal(quote.total))
    quote.status = "refunded"
    db.add(LedgerEntry(user_id=user_id, amount=quote.total, balance_after=wallet.balance, entry_type="refund", reference_id=str(quote.id), description=reason))
    return quote
