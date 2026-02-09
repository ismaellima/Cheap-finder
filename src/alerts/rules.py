from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AlertEvent, AlertRule, Brand, Notification, Product

logger = logging.getLogger(__name__)


async def check_price_alert(
    session: AsyncSession,
    product: Product,
    old_price: int,
    new_price: int,
) -> list[AlertEvent]:
    if old_price <= 0 or new_price >= old_price:
        return []

    pct_change = ((old_price - new_price) / old_price) * 100
    events: list[AlertEvent] = []

    rules = await session.execute(
        select(AlertRule).where(
            AlertRule.active.is_(True),
            (
                (AlertRule.product_id == product.id)
                | (AlertRule.brand_id == product.brand_id)
                | (AlertRule.brand_id.is_(None) & AlertRule.product_id.is_(None))
            ),
        )
    )

    for rule in rules.scalars().all():
        triggered = False

        if rule.condition == "any_sale" and new_price < old_price:
            triggered = True
        elif rule.condition == "pct_drop" and pct_change >= rule.threshold_pct:
            triggered = True
        elif rule.condition == "absolute_drop" and (old_price - new_price) >= rule.threshold_amount:
            triggered = True

        if triggered:
            event = AlertEvent(
                rule_id=rule.id,
                product_id=product.id,
                old_price=old_price,
                new_price=new_price,
                pct_change=round(pct_change, 1),
            )
            session.add(event)
            await session.flush()

            if rule.notify_dashboard:
                brand_name = product.brand.name if product.brand else "Unknown"
                notification = Notification(
                    alert_event_id=event.id,
                    title=f"Price drop: {product.name}",
                    message=(
                        f"{brand_name} — {product.name} dropped {pct_change:.0f}% "
                        f"(${old_price / 100:.2f} → ${new_price / 100:.2f})"
                    ),
                )
                session.add(notification)

            events.append(event)
            logger.info(
                f"Alert triggered: {product.name} dropped {pct_change:.1f}% "
                f"(rule {rule.id})"
            )

    if events:
        await session.commit()

    return events


async def create_default_rule_for_brand(
    session: AsyncSession, brand: Brand
) -> AlertRule:
    rule = AlertRule(
        brand_id=brand.id,
        condition="pct_drop",
        threshold_pct=brand.alert_threshold_pct,
        notify_email=True,
        notify_dashboard=True,
    )
    session.add(rule)
    await session.commit()
    return rule
