import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from database import AsyncSessionLocal
from models import GlucoseReading
from libre import libre_client

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/Bahia")


def parse_timestamp_utc(ts_str: str) -> datetime:
    """Converte timestamp BRT (vindo do libre.py) de volta para UTC para salvar no banco."""
    from datetime import timedelta
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M:%S %p"]
    for fmt in formats:
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt + timedelta(hours=3)  # BRT → UTC
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.utcnow()


async def poll_libre():
    try:
        readings = await libre_client.get_graph()
        if not readings:
            latest = await libre_client.get_latest_reading()
            if latest:
                readings = [latest]

        if not readings:
            logger.warning("Nenhuma leitura recebida do Nightscout")
            return

        async with AsyncSessionLocal() as db:
            saved = 0
            for r in readings:
                if not r.get("timestamp") or not r.get("value_mgdl"):
                    continue

                ts = parse_timestamp_utc(str(r["timestamp"]))

                existing = await db.execute(
                    select(GlucoseReading).where(GlucoseReading.timestamp == ts)
                )
                if existing.scalar_one_or_none():
                    continue

                db.add(GlucoseReading(
                    value_mgdl=r["value_mgdl"],
                    timestamp=ts,
                    trend=r.get("trend"),
                    is_high=r.get("is_high", False),
                    is_low=r.get("is_low", False),
                ))
                saved += 1

            await db.commit()
            logger.info(f"Nightscout polling: {saved} novas leituras salvas")

    except Exception as e:
        logger.error(f"Erro no polling: {e}", exc_info=True)


def start_scheduler():
    scheduler.add_job(poll_libre, "interval", minutes=5, id="poll_libre", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler iniciado — polling a cada 5 minutos")
