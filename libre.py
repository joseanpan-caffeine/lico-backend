import httpx
import logging
import os
import json
import base64
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LIBRE_API_URL", "https://api-eu.libreview.io")

HEADERS = {
    "product": "llu.android",
    "version": "4.16.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
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

    async def login(self, _redirect_count: int = 0) -> bool:
        if _redirect_count > 2:
            raise RuntimeError("Too many redirects")

        global BASE_URL
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/llu/auth/login",
                headers=HEADERS,
                json={"email": self.email, "password": self.password},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            if data.get("data", {}).get("redirect"):
                region = data["data"]["region"]
                BASE_URL = f"https://api-{region}.libreview.io"
                logger.info(f"Redirect to region: {region}")
                return await self.login(_redirect_count + 1)

            if data.get("status") == 2:
                await self._accept_terms()
                return await self.login(_redirect_count + 1)

            self.token = data["data"]["authTicket"]["token"]
            jwt = decode_jwt(self.token)
            self.account_id = jwt.get("id") or data["data"]["user"]["id"]
            self.patient_id = self.account_id  # conta paciente: ID próprio
            logger.info(f"Login OK | role: {jwt.get('role')} | id: {self.account_id}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/llu/auth/continue/tou",
                headers=HEADERS, json={}, timeout=10,
            )

    def _build_headers(self) -> dict:
        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        if self.account_id:
            headers["account-id"] = self.account_id
        return headers

    async def _authed_get(self, path: str, _retried: bool = False) -> dict:
        if not self.token:
            await self.login()

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}{path}",
                headers=self._build_headers(),
                timeout=15,
            )

        if not r.is_success:
            logger.error(f"{r.status_code} {path} | {r.text[:300]}")

        if r.status_code == 401 and not _retried:
            self.token = None
            await self.login()
            return await self._authed_get(path, _retried=True)

        r.raise_for_status()
        return r.json()

    async def get_latest_reading(self) -> Optional[dict]:
        """Busca última leitura — funciona para conta paciente."""
        try:
            # Endpoint 1: histórico de glucose do próprio paciente
            data = await self._authed_get("/llu/glucoseHistory")
            readings = data.get("data", {}).get("glucoseHistory", [])
            if readings:
                latest = readings[-1]
                logger.info(f"Leitura via glucoseHistory: {latest}")
                return {
                    "value_mgdl": latest.get("ValueInMgPerDl") or latest.get("value"),
                    "timestamp": latest.get("Timestamp") or latest.get("timestamp"),
                    "trend": latest.get("TrendArrow") or latest.get("trend"),
                    "is_high": latest.get("isHigh", False),
                    "is_low": latest.get("isLow", False),
                }
        except Exception as e:
            logger.warning(f"glucoseHistory falhou: {e}")

        try:
            # Endpoint 2: leitura atual via sensor ativo
            data = await self._authed_get("/llu/sensor/reading")
            r = data.get("data", {})
            if r:
                logger.info(f"Leitura via sensor/reading: {r}")
                return {
                    "value_mgdl": r.get("ValueInMgPerDl") or r.get("value"),
                    "timestamp": r.get("Timestamp") or r.get("timestamp"),
                    "trend": r.get("TrendArrow") or r.get("trend"),
                    "is_high": r.get("isHigh", False),
                    "is_low": r.get("isLow", False),
                }
        except Exception as e:
            logger.warning(f"sensor/reading falhou: {e}")

        try:
            # Endpoint 3: dashboard do paciente
            data = await self._authed_get("/llu/patient/dashboard")
            g = data.get("data", {}).get("glucoseMeasurement", {})
            if g:
                logger.info(f"Leitura via patient/dashboard: {g}")
                return {
                    "value_mgdl": g.get("ValueInMgPerDl"),
                    "timestamp": g.get("Timestamp"),
                    "trend": g.get("TrendArrow"),
                    "is_high": g.get("isHigh", False),
                    "is_low": g.get("isLow", False),
                }
        except Exception as e:
            logger.warning(f"patient/dashboard falhou: {e}")

        logger.error("Todos os endpoints falharam — logando resposta completa para diagnóstico")
        try:
            data = await self._authed_get("/llu/glucoseHistory")
            logger.info(f"glucoseHistory raw: {json.dumps(data)[:500]}")
        except Exception as e:
            logger.error(f"glucoseHistory raw error: {e}")

        return None

    async def get_graph(self) -> list[dict]:
        """Busca histórico das últimas horas."""
        try:
            data = await self._authed_get("/llu/glucoseHistory")
            readings = data.get("data", {}).get("glucoseHistory", [])
            if readings:
                logger.info(f"Graph: {len(readings)} leituras via glucoseHistory")
                return [
                    {
                        "value_mgdl": r.get("ValueInMgPerDl") or r.get("value"),
                        "timestamp": r.get("Timestamp") or r.get("timestamp"),
                        "trend": r.get("TrendArrow") or r.get("trend"),
                        "is_high": False,
                        "is_low": False,
                    }
                    for r in readings
                    if (r.get("ValueInMgPerDl") or r.get("value")) is not None
                ]
        except Exception as e:
            logger.warning(f"get_graph glucoseHistory falhou: {e}")

        # Fallback: retorna leitura única como lista
        latest = await self.get_latest_reading()
        if latest:
            return [latest]
        return []


libre_client = LibreClient()
