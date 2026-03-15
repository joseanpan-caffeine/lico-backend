import httpx
import os
from datetime import datetime
from typing import Optional

BASE_URL = os.getenv("LIBRE_API_URL", "https://api-eu.libreview.io")

HEADERS = {
    "product": "llu.ios",
    "version": "4.12.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class LibreClient:
    def __init__(self):
        self.email = os.getenv("LIBRE_EMAIL")
        self.password = os.getenv("LIBRE_PASSWORD")
        self.token: Optional[str] = None
        self.patient_id: Optional[str] = None

    async def login(self, _redirect_count: int = 0) -> bool:
        """Autentica na Abbott. Trata redirect de região com limite de tentativas."""
        if _redirect_count > 2:
            raise RuntimeError("LibreLinkUp: muitos redirects de região, verifique LIBRE_API_URL")

        global BASE_URL
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/llu/auth/login",
                headers=HEADERS,
                json={"email": self.email, "password": self.password},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()

            if data.get("data", {}).get("redirect"):
                region = data["data"]["region"]
                BASE_URL = f"https://api-{region}.libreview.io"
                return await self.login(_redirect_count=_redirect_count + 1)

            self.token = data["data"]["authTicket"]["token"]
            return True

    async def _authed_get(self, path: str, _retried: bool = False) -> dict:
        """GET autenticado com um retry automático em caso de 401."""
        if not self.token:
            await self.login()

        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{path}", headers=headers, timeout=10)

        if r.status_code == 401 and not _retried:
            # Token expirou — faz login e tenta uma vez mais
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
        """Últimas ~12h de leituras do gráfico do sensor."""
        # Garante que temos o patient_id
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
