import httpx
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LIBRE_API_URL", "https://api-eu.libreview.io")

HEADERS = {
    "product": "llu.android",
    "version": "4.16.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class LibreClient:
    def __init__(self):
        self.email = os.getenv("LIBRE_EMAIL")
        self.password = os.getenv("LIBRE_PASSWORD")
        self.token: Optional[str] = None
        self.account_id: Optional[str] = None
        self.patient_id: Optional[str] = None

    async def login(self, _redirect_count: int = 0) -> bool:
        if _redirect_count > 2:
            raise RuntimeError("LibreLinkUp: muitos redirects de região")

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
                logger.info(f"Redirecionando para região: {region}")
                return await self.login(_redirect_count=_redirect_count + 1)

            if data.get("status") == 2:
                await self._accept_terms()
                return await self.login(_redirect_count=_redirect_count + 1)

            auth_ticket = data.get("data", {}).get("authTicket", {})
            self.token = auth_ticket.get("token")

            user_data = data.get("data", {}).get("user", {})
            # Loga todos os campos do user para inspecionar
            logger.info(f"User fields: {list(user_data.keys())}")
            logger.info(f"User data: {user_data}")

            # Tenta diferentes campos onde o account-id pode estar
            self.account_id = (
                user_data.get("id")
                or user_data.get("accountId")
                or user_data.get("account_id")
            )

            # Remove hífens para enviar no header
            if self.account_id:
                self.account_id_clean = self.account_id.replace("-", "")
            else:
                self.account_id_clean = None

            logger.info(f"Login OK | account_id raw: {self.account_id} | clean: {self.account_id_clean}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/llu/auth/continue/tou",
                headers=HEADERS,
                json={},
                timeout=10,
            )
            logger.info(f"Termos aceitos: {r.status_code}")

    def _build_headers(self) -> dict:
        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        # Testa com e sem hífens — loga qual está sendo enviado
        if self.account_id_clean:
            headers["account-id"] = self.account_id_clean
            logger.info(f"Enviando account-id (sem hífens): {self.account_id_clean}")
        elif self.account_id:
            headers["account-id"] = self.account_id
            logger.info(f"Enviando account-id (com hífens): {self.account_id}")
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
            logger.error(f"LibreAPI {r.status_code} em {path} | corpo: {r.text[:500]}")

        if r.status_code == 401 and not _retried:
            self.token = None
            await self.login()
            return await self._authed_get(path, _retried=True)

        r.raise_for_status()
        return r.json()

    async def get_connections(self) -> list:
        data = await self._authed_get("/llu/connections")
        return data.get("data", [])

    async def get_latest_reading(self) -> Optional[dict]:
        connections = await self.get_connections()
        if not connections:
            return None

        conn = connections[0]
        self.patient_id = conn.get("patientId")

        glucose_data = conn.get("glucoseMeasurement", {})
        if not glucose_data:
            return None

        return {
            "value_mgdl": glucose_data.get("ValueInMgPerDl"),
            "timestamp": glucose_data.get("Timestamp"),
            "trend": glucose_data.get("TrendArrow"),
            "is_high": glucose_data.get("isHigh", False),
            "is_low": glucose_data.get("isLow", False),
        }

    async def get_graph(self) -> list[dict]:
        if not self.patient_id:
            await self.get_latest_reading()
        if not self.patient_id:
            return []

        data = await self._authed_get(f"/llu/connections/{self.patient_id}/graph")
        graph_data = data.get("data", {}).get("graphData", [])

        return [
            {
                "value_mgdl": item.get("ValueInMgPerDl"),
                "timestamp": item.get("Timestamp"),
                "trend": item.get("TrendArrow"),
                "is_high": False,
                "is_low": False,
            }
            for item in graph_data
            if item.get("ValueInMgPerDl") is not None
        ]


# Singleton
libre_client = LibreClient()
