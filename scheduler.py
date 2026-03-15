import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from database import AsyncSessionLocal
from models import GlucoseReading
from libre import libre_client

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/Bahia")


def parse_libre_timestamp(ts_str: str) -> datetime:
    """Converte o formato da Abbott: 'M/D/YYYY H:MM:SS AM/PM'"""
    try:
        return datetime.strptime(ts_str, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        # Fallback para outros formatos possíveis
        return datetime.fromisoformat(ts_str)


async def poll_libre():
    """Roda a cada 5 minutos: busca leituras do Libre e persiste novas."""
    try:
        readings = await libre_client.get_graph()
        if not readings:
            # Fallback para leitura única se o gráfico falhar
            latest = await libre_client.get_latest_reading()
            if latest:
                readings = [latest]

        if not readings:
            logger.warning("Nenhuma leitura recebida do LibreLinkUp")
            return

        async with AsyncSessionLocal() as db:
            saved = 0
            for r in readings:
                ts = parse_libre_timestamp(r["timestamp"])

                # Evita duplicatas por timestamp
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
            logger.info(f"Polling Libre: {saved} novas leituras salvas")

    except Exception as e:
        logger.error(f"Erro no polling Libre: {e}", exc_info=True)


def start_scheduler():
    scheduler.add_job(poll_libre, "interval", minutes=5, id="libre_poll", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler iniciado — polling a cada 5 minutos")
