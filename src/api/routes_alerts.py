from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import AlertEvent, AlertRule, Notification
from src.db.session import get_session

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertRuleCreate(BaseModel):
    brand_id: int | None = None
    product_id: int | None = None
    condition: str = "pct_drop"
    threshold_pct: float = 10.0
    threshold_amount: int = 0
    notify_email: bool = True
    notify_dashboard: bool = True


@router.get("/rules")
async def list_rules(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(AlertRule)
        .options(selectinload(AlertRule.brand))
        .order_by(AlertRule.created_at.desc())
    )
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "brand": r.brand.name if r.brand else "All brands",
            "brand_id": r.brand_id,
            "product_id": r.product_id,
            "condition": r.condition,
            "threshold_pct": r.threshold_pct,
            "threshold_amount": r.threshold_amount,
            "notify_email": r.notify_email,
            "notify_dashboard": r.notify_dashboard,
            "active": r.active,
        }
        for r in rules
    ]


@router.post("/rules")
async def create_rule(
    data: AlertRuleCreate, session: AsyncSession = Depends(get_session)
):
    rule = AlertRule(**data.model_dump())
    session.add(rule)
    await session.commit()
    return {"id": rule.id, "created": True}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    rule = await session.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    await session.delete(rule)
    await session.commit()
    return {"deleted": True}


@router.get("/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    query = select(Notification).options(
        selectinload(Notification.alert_event).selectinload(AlertEvent.product)
    )
    if unread_only:
        query = query.where(Notification.read.is_(False))
    query = query.order_by(Notification.created_at.desc()).limit(limit)

    result = await session.execute(query)
    notifications = result.scalars().all()

    return [
        {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "read": n.read,
            "created_at": n.created_at.isoformat(),
            "product_id": n.alert_event.product_id if n.alert_event else None,
        }
        for n in notifications
    ]


@router.get("/notifications/count")
async def unread_count(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    return {"unread": result.scalar() or 0}


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: int, session: AsyncSession = Depends(get_session)
):
    notification = await session.get(Notification, notification_id)
    if not notification:
        raise HTTPException(404, "Notification not found")
    notification.read = True
    await session.commit()
    return {"read": True}


@router.post("/notifications/read-all")
async def mark_all_read(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Notification).where(Notification.read.is_(False))
    )
    for n in result.scalars().all():
        n.read = True
    await session.commit()
    return {"done": True}
