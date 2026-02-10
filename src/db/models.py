from __future__ import annotations

import datetime as dt
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    aliases: Mapped[str] = mapped_column(Text, default="")  # JSON list stored as text
    category: Mapped[str] = mapped_column(String(100), default="")
    alert_threshold_pct: Mapped[float] = mapped_column(Float, default=10.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    brand_retailers: Mapped[List[BrandRetailer]] = relationship(
        back_populates="brand", cascade="all, delete-orphan"
    )
    products: Mapped[List[Product]] = relationship(
        back_populates="brand", cascade="all, delete-orphan"
    )
    alert_rules: Mapped[List[AlertRule]] = relationship(
        back_populates="brand", cascade="all, delete-orphan"
    )


class Retailer(Base):
    __tablename__ = "retailers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    scraper_type: Mapped[str] = mapped_column(String(50), default="generic")
    requires_js: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    brand_retailers: Mapped[List[BrandRetailer]] = relationship(
        back_populates="retailer", cascade="all, delete-orphan"
    )
    products: Mapped[List[Product]] = relationship(
        back_populates="retailer", cascade="all, delete-orphan"
    )


class BrandRetailer(Base):
    __tablename__ = "brand_retailers"
    __table_args__ = (
        UniqueConstraint("brand_id", "retailer_id", name="uq_brand_retailer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), nullable=False)
    retailer_id: Mapped[int] = mapped_column(
        ForeignKey("retailers.id"), nullable=False
    )
    brand_url: Mapped[str] = mapped_column(String(500), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    brand: Mapped[Brand] = relationship(back_populates="brand_retailers")
    retailer: Mapped[Retailer] = relationship(back_populates="brand_retailers")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("url", name="uq_product_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), nullable=False)
    retailer_id: Mapped[int] = mapped_column(
        ForeignKey("retailers.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    image_url: Mapped[str] = mapped_column(String(1000), default="")
    thumbnail_url: Mapped[str] = mapped_column(String(1000), default="")
    sku: Mapped[str] = mapped_column(String(200), default="")
    gender: Mapped[str] = mapped_column(String(20), default="")  # men, women, unisex, or empty
    sizes: Mapped[str] = mapped_column(Text, default="")  # JSON list e.g. ["S","M","L"]
    current_price: Mapped[int] = mapped_column(Integer, nullable=True)  # cents CAD
    original_price: Mapped[int] = mapped_column(Integer, nullable=True)  # cents CAD
    on_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    brand: Mapped[Brand] = relationship(back_populates="products")
    retailer: Mapped[Retailer] = relationship(back_populates="products")
    price_records: Mapped[List[PriceRecord]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PriceRecord(Base):
    __tablename__ = "price_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), nullable=False
    )
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # cents CAD
    original_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    on_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    product: Mapped[Product] = relationship(back_populates="price_records")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("brands.id"), nullable=True
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    condition: Mapped[str] = mapped_column(
        String(50), default="pct_drop"
    )  # pct_drop | absolute_drop | any_sale
    threshold_pct: Mapped[float] = mapped_column(Float, default=10.0)
    threshold_amount: Mapped[int] = mapped_column(Integer, default=0)  # cents
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_dashboard: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    brand: Mapped[Optional[Brand]] = relationship(back_populates="alert_rules")
    alert_events: Mapped[List[AlertEvent]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), nullable=False
    )
    old_price: Mapped[int] = mapped_column(Integer, nullable=False)
    new_price: Mapped[int] = mapped_column(Integer, nullable=False)
    pct_change: Mapped[float] = mapped_column(Float, nullable=False)
    sent_email: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    rule: Mapped[AlertRule] = relationship(back_populates="alert_events")
    product: Mapped[Product] = relationship()
    notifications: Mapped[List[Notification]] = relationship(
        back_populates="alert_event", cascade="all, delete-orphan"
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_event_id: Mapped[int] = mapped_column(
        ForeignKey("alert_events.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )

    alert_event: Mapped[AlertEvent] = relationship(back_populates="notifications")


class RetailerSuggestion(Base):
    __tablename__ = "retailer_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending | approved | failed
    health_check_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    health_check_message: Mapped[str] = mapped_column(Text, default="")
    retailer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("retailers.id"), nullable=True
    )  # set when approved and a Retailer is created
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )
