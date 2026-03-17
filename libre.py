import httpx
import logging
import os
from typing import Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NIGHTSCOUT_URL = os.getenv("NIGHTSCOUT_URL", "")
NIGHTSCOUT_API_TOKEN = os.getenv("NIGHTSCOUT_API_TOKEN", "")


class LibreClient:
    def __init__(self):
        self.nightscout_url = NIGHTSCOUT_URL.rstrip("/")
        self.token = NIGHTSCOUT_API_TOKEN

    def _headers(self) -> dict:
        return {
            "api-secret": self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def get_latest_reading(self) -> Optional[dict]:
        try:
            url = f"{self.nightscout_url}/api/v1/entries/sgv.json?count=1"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            entries = r.json()
            if not entries:
                logger.warning("Nightscout: nenhuma leitura encontrada")
                return None

            entry = entries[0]
            sgv = entry.get("sgv")
            direction = entry.get("direction", "")
            date_ms = entry.get("date", 0)

            # Converte direção para número (compatível com o resto do app)
            trend_map = {
                "DoubleUp": 5, "SingleUp": 4, "FortyFiveUp": 4,
                "Flat": 3,
                "FortyFiveDown": 2, "SingleDown": 2, "DoubleDown": 1,
            }
            trend = trend_map.get(direction, 3)

            timestamp = datetime.fromtimestamp(date_ms / 1000, tz=ZoneInfo("America/Bahia")).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

            logger.info(f"Nightscout: {sgv} mg/dL | {direction} | {timestamp}")

            return {
                "value_mgdl": sgv,
                "timestamp": timestamp,
                "trend": trend,
                "is_high": sgv > 180 if sgv else False,
                "is_low": sgv < 70 if sgv else False,
            }
        except Exception as e:
            logger.error(f"get_latest_reading falhou: {e}")
            return None

    async def get_graph(self) -> list[dict]:
        try:
            url = f"{self.nightscout_url}/api/v1/entries/sgv.json?count=288"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            entries = r.json()

            trend_map = {
                "DoubleUp": 5, "SingleUp": 4, "FortyFiveUp": 4,
                "Flat": 3,
                "FortyFiveDown": 2, "SingleDown": 2, "DoubleDown": 1,
            }

            readings = []
            for entry in entries:
                sgv = entry.get("sgv")
                if not sgv:
                    continue
                direction = entry.get("direction", "")
                date_ms = entry.get("date", 0)
                timestamp = datetime.fromtimestamp(date_ms / 1000, tz=ZoneInfo("America/Bahia")).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
                readings.append({
                    "value_mgdl": sgv,
                    "timestamp": timestamp,
                    "trend": trend_map.get(direction, 3),
                    "is_high": sgv > 180,
                    "is_low": sgv < 70,
                })

            logger.info(f"Nightscout graph: {len(readings)} leituras")
            return readings
        except Exception as e:
            logger.error(f"get_graph falhou: {e}")
            return []


libre_client = LibreClient()
