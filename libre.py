import httpx
import logging
import os
import json
import base64
from typing import Optional

logger = logging.getLogger(__name__)

# API base — usa o regional para autenticação, mas glucoseHistory usa api global
BASE_URL = os.getenv("LIBRE_API_URL", "https://api-eu.libreview.io")
GLUCOSE_URL = "https://api.libreview.io"

# Headers para autenticação LLU
LOGIN_HEADERS = {
    "product": "llu.android",
    "version": "4.16.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "cache-control": "no-cache",
}


def decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (4 - len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception as e:
        logger.error(f"JWT decode error: {e}")
        return {}


class LibreClient:
    def __init__(self):
        self.email = os.getenv("LIBRE_EMAIL")
        self.password = os.getenv("LIBRE_PASSWORD")
        self.token: Optional[str] = None
        self.account_id: Optional[str] = None
        self.patient_id: Optional[str] = None
        self.region: Optional[str] = None

    async def login(self, _redirect_count: int = 0) -> bool:
        if _redirect_count > 2:
            raise RuntimeError("Too many redirects")

        global BASE_URL
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/llu/auth/login",
                headers=LOGIN_HEADERS,
                json={"email": self.email, "password": self.password},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            logger.info(f"Login status: {data.get('status')} | region: {data.get('data', {}).get('region', 'n/a')}")

            if data.get("data", {}).get("redirect"):
                region = data["data"]["region"]
                self.region = region
                BASE_URL = f"https://api-{region}.libreview.io"
                logger.info(f"Redirect to region: {region} → {BASE_URL}")
                return await self.login(_redirect_count + 1)

            if data.get("status") == 2:
                await self._accept_terms()
                return await self.login(_redirect_count + 1)

            auth = data.get("data", {}).get("authTicket", {})
            if not auth:
                raise RuntimeError(f"authTicket não encontrado: {data}")

            self.token = auth.get("token")
            jwt = decode_jwt(self.token)
            raw_id = jwt.get("id") or data.get("data", {}).get("user", {}).get("id", "")
            self.account_id = raw_id.replace("-", "")
            self.patient_id = raw_id
            self.region = self.region or jwt.get("region", "la")

            logger.info(f"Login OK | role: {jwt.get('role')} | region: {self.region} | id: {raw_id}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/llu/auth/continue/tou",
                headers=LOGIN_HEADERS, json={}, timeout=10,
            )

    async def _get_glucose_history(self) -> list[dict]:
        """
        Usa endpoint /glucoseHistory da API do LibreView diretamente.
        Funciona com credenciais do próprio paciente.
        """
        if not self.token:
            await self.login()

        # Tenta com URL regional primeiro, depois global
        urls = [
            f"https://api-{self.region}.libreview.io/glucoseHistory?numPeriods=1&period=1",
            f"{GLUCOSE_URL}/glucoseHistory?numPeriods=1&period=1",
            f"https://api-{self.region}.libreview.io/llu/glucoseHistory?numPeriods=1&period=1",
        ]

        headers = {
            **LOGIN_HEADERS,
            "Authorization": f"Bearer {self.token}",
            "account-id": self.account_id,
        }

        for url in urls:
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(url, headers=headers, timeout=15)
                logger.info(f"glucoseHistory {url} → {r.status_code} | {r.text[:200]}")
                if r.is_success:
                    return r.json()
            except Exception as e:
                logger.warning(f"glucoseHistory {url} falhou: {e}")

        return {}

    async def get_latest_reading(self) -> Optional[dict]:
        if not self.token:
            await self.login()

        # Tenta glucoseHistory (funciona com conta patient)
        data = await self._get_glucose_history()
        if data:
            logger.info(f"glucoseHistory retornou: {json.dumps(data)[:400]}")
            # Extrai a leitura mais recente da estrutura
            periods = data.get("data", {}).get("periods", [])
            if periods:
                entries = periods[0].get("data", []) or periods[0].get("entries", [])
                if entries:
                    latest = entries[-1]
                    return {
                        "value_mgdl": latest.get("ValueInMgPerDl") or latest.get("value"),
                        "timestamp": latest.get("Timestamp") or latest.get("timestamp"),
                        "trend": latest.get("TrendArrow") or latest.get("trend"),
                        "is_high": (latest.get("ValueInMgPerDl") or latest.get("value") or 0) > 180,
                        "is_low": (latest.get("ValueInMgPerDl") or latest.get("value") or 0) < 70,
                    }

        return None

    async def get_graph(self) -> list[dict]:
        if not self.token:
            await self.login()

        data = await self._get_glucose_history()
        if not data:
            return []

        logger.info(f"glucoseHistory full: {json.dumps(data)[:600]}")

        readings = []
        periods = data.get("data", {}).get("periods", [])
        for period in periods:
            entries = period.get("data", []) or period.get("entries", []) or period.get("continuous_glucose", [])
            for e in entries:
                val = e.get("ValueInMgPerDl") or e.get("value")
                ts = e.get("Timestamp") or e.get("timestamp")
                if val and ts:
                    readings.append({
                        "value_mgdl": val,
                        "timestamp": str(ts),
                        "trend": e.get("TrendArrow") or e.get("trend"),
                        "is_high": val > 180,
                        "is_low": val < 70,
                    })

        logger.info(f"Graph: {len(readings)} leituras extraídas")
        return readings


libre_client = LibreClient()
