from datetime import datetime
from typing import Optional
from sqlalchemy import Column, Integer, Float, String, DateTime, Boolean
from sqlalchemy.sql import func
from pydantic import BaseModel
from database import Base


# ─── ORM Models ───────────────────────────────────────────────────────────────

class GlucoseReading(Base):
    __tablename__ = "glucose_readings"

    id = Column(Integer, primary_key=True, index=True)
    value_mgdl = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    trend = Column(Integer, nullable=True)       # 1=caindo rápido … 5=subindo rápido
    is_high = Column(Boolean, default=False)
    is_low = Column(Boolean, default=False)
    source = Column(String, default="libre2")    # para o futuro (manual, etc.)
    created_at = Column(DateTime, server_default=func.now())


# ─── Pydantic Schemas ─────────────────────────────────────────────────────────

class GlucoseReadingOut(BaseModel):
    id: int
    value_mgdl: float
    timestamp: datetime
    trend: Optional[int]
    is_high: bool
    is_low: bool

    model_config = {"from_attributes": True}


class GlucoseSummary(BaseModel):
    current: Optional[GlucoseReadingOut]
    last_24h: list[GlucoseReadingOut]
    avg_24h: Optional[float]
    time_in_range_pct: Optional[float]    # 70–180 mg/dL
    readings_count: int


# ─── Meal Models ──────────────────────────────────────────────────────────────

class MealEntry(Base):
    __tablename__ = "meal_entries"

    id = Column(Integer, primary_key=True, index=True)
    food_name = Column(String, nullable=False)
    medida = Column(String, nullable=True)
    quantity = Column(Float, nullable=False, default=1.0)   # múltiplo da medida
    cho_g = Column(Float, nullable=False)                    # CHO total (qty * cho_unit)
    cho_unit_g = Column(Float, nullable=False)               # CHO da medida base
    kcal = Column(Float, nullable=True)
    grupo = Column(String, nullable=True)
    eaten_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())


class MealEntryIn(BaseModel):
    food_name: str
    medida: Optional[str] = None
    quantity: float = 1.0
    cho_unit_g: float
    kcal: Optional[float] = None
    grupo: Optional[str] = None
    eaten_at: Optional[datetime] = None   # defaults to now


class MealEntryOut(BaseModel):
    id: int
    food_name: str
    medida: Optional[str]
    quantity: float
    cho_g: float
    cho_unit_g: float
    kcal: Optional[float]
    grupo: Optional[str]
    eaten_at: datetime

    model_config = {"from_attributes": True}


class DailySummary(BaseModel):
    meals: list[MealEntryOut]
    total_cho_g: float
    total_kcal: float
