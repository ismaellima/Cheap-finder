from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from src.config import settings
from src.db.models import AlertEvent

logger = logging.getLogger(__name__)


async def send_email_alert(event: AlertEvent) -> bool:
    if not all([settings.SMTP_HOST, settings.SMTP_USER, settings.SMTP_PASS, settings.ALERT_EMAIL_TO]):
        logger.warning("Email not configured — skipping email alert")
        return False

    product = event.product
    brand_name = product.brand.name if product.brand else "Unknown"

    subject = f"Price Drop: {brand_name} — {product.name} (-{event.pct_change:.0f}%)"
    body = (
        f"Price drop detected!\n\n"
        f"Brand: {brand_name}\n"
        f"Product: {product.name}\n"
        f"Retailer: {product.retailer.name if product.retailer else 'Unknown'}\n\n"
        f"Old price: ${event.old_price / 100:.2f} CAD\n"
        f"New price: ${event.new_price / 100:.2f} CAD\n"
        f"Drop: {event.pct_change:.1f}%\n\n"
        f"Link: {product.url}\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_USER
    msg["To"] = settings.ALERT_EMAIL_TO
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASS,
            start_tls=True,
        )
        logger.info(f"Email alert sent for {product.name}")
        return True
    except Exception:
        logger.exception(f"Failed to send email alert for {product.name}")
        return False


async def send_alert(event: AlertEvent) -> None:
    rule = event.rule

    if rule.notify_email and not event.sent_email:
        success = await send_email_alert(event)
        if success:
            event.sent_email = True
