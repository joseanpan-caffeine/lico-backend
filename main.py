import logging
import json as _json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import init_db, get_db
from models import GlucoseReading, GlucoseReadingOut, GlucoseSummary
from models import MealEntry, MealEntryIn, MealEntryOut, DailySummary
from scheduler import start_scheduler, poll_libre

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await poll_libre()
    start_scheduler()
    yield


app = FastAPI(title="Glico API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/glucose/latest", response_model=Optional[GlucoseReadingOut])
async def get_latest(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(GlucoseReading).order_by(GlucoseReading.timestamp.desc()).limit(1)
    )
    return result.scalar_one_or_none()


@app.get("/glucose/history", response_model=list[GlucoseReadingOut])
async def get_history(
    hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(GlucoseReading)
        .where(GlucoseReading.timestamp >= since)
        .order_by(GlucoseReading.timestamp.asc())
    )
    return result.scalars().all()


@app.get("/glucose/summary", response_model=GlucoseSummary)
async def get_summary(db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(hours=24)

    latest_result = await db.execute(
        select(GlucoseReading).order_by(GlucoseReading.timestamp.desc()).limit(1)
    )
    current = latest_result.scalar_one_or_none()

    history_result = await db.execute(
        select(GlucoseReading)
        .where(GlucoseReading.timestamp >= since)
        .order_by(GlucoseReading.timestamp.asc())
    )
    readings_24h = history_result.scalars().all()

    avg_24h = None
    time_in_range_pct = None

    if readings_24h:
        values = [r.value_mgdl for r in readings_24h]
        avg_24h = round(sum(values) / len(values), 1)
        in_range = [v for v in values if 70 <= v <= 180]
        time_in_range_pct = round(len(in_range) / len(values) * 100, 1)

    return GlucoseSummary(
        current=current,
        last_24h=readings_24h,
        avg_24h=avg_24h,
        time_in_range_pct=time_in_range_pct,
        readings_count=len(readings_24h),
    )


@app.post("/glucose/sync")
async def force_sync():
    await poll_libre()
    return {"message": "Sync disparado"}


# ─── Admin ────────────────────────────────────────────────────────────────────

@app.delete("/admin/clear-glucose")
async def clear_glucose(db: AsyncSession = Depends(get_db)):
    await db.execute(text("TRUNCATE TABLE glucose_readings RESTART IDENTITY"))
    await db.commit()
    return {"status": "cleared"}


# ─── Food Search ──────────────────────────────────────────────────────────────

_FOODS_PATH = Path(__file__).parent / "foods.json"
_foods_db: list[dict] = _json.loads(_FOODS_PATH.read_text(encoding="utf-8"))


@app.get("/foods/search")
async def search_foods(q: str = Query(..., min_length=2)):
    q_lower = q.lower().strip()
    starts = [f for f in _foods_db if f["nome"].lower().startswith(q_lower)]
    contains = [f for f in _foods_db if q_lower in f["nome"].lower() and f not in starts]
    return (starts + contains)[:20]


# ─── Meals ────────────────────────────────────────────────────────────────────

@app.post("/meals", response_model=MealEntryOut, status_code=201)
async def log_meal(entry: MealEntryIn, db: AsyncSession = Depends(get_db)):
    eaten_at = entry.eaten_at or datetime.utcnow()
    cho_total = round(entry.cho_unit_g * entry.quantity, 1)
    kcal_total = round(entry.kcal * entry.quantity, 1) if entry.kcal else None

    meal = MealEntry(
        food_name=entry.food_name,
        medida=entry.medida,
        quantity=entry.quantity,
        cho_g=cho_total,
        cho_unit_g=entry.cho_unit_g,
        kcal=kcal_total,
        grupo=entry.grupo,
        eaten_at=eaten_at,
    )
    db.add(meal)
    await db.commit()
    await db.refresh(meal)
    return meal


@app.get("/meals/today", response_model=DailySummary)
async def get_today_meals(db: AsyncSession = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(MealEntry)
        .where(MealEntry.eaten_at >= today_start)
        .order_by(MealEntry.eaten_at.asc())
    )
    meals = result.scalars().all()
    total_cho = round(sum(m.cho_g for m in meals), 1)
    total_kcal = round(sum(m.kcal or 0 for m in meals), 1)
    return DailySummary(meals=meals, total_cho_g=total_cho, total_kcal=total_kcal)


@app.delete("/meals/{meal_id}", status_code=204)
async def delete_meal(meal_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(MealEntry).where(MealEntry.id == meal_id))
    meal = result.scalar_one_or_none()
    if not meal:
        raise HTTPException(status_code=404, detail="Refeição não encontrada")
    await db.delete(meal)
    await db.commit()


# ─── Meal Parser ──────────────────────────────────────────────────────────────

from meal_parser import parse_meal_text
from pydantic import BaseModel as _BaseModel


class MealParseRequest(_BaseModel):
    description: str


@app.post("/meals/parse")
async def parse_meal(body: MealParseRequest):
    if not body.description.strip():
        return []
    return await parse_meal_text(body.description)
