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
            logger.info(f"JWT: {jwt}")

            # account-id: usa o do JWT com hifens, exatamente como veio
            self.account_id = jwt.get("id") or data["data"]["user"]["id"]
            logger.info(f"account_id final: {self.account_id}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            await client.post(f"{BASE_URL}/llu/auth/continue/tou", headers=HEADERS, json={}, timeout=10)

    async def _authed_get(self, path: str, _retried: bool = False) -> dict:
        if not self.token:
            await self.login()

        # Envia account-id com hifens — exatamente como está no JWT
        headers = {
            **HEADERS,
            "Authorization": f"Bearer {self.token}",
            "account-id": self.account_id,
        }
        logger.info(f"GET {path} | account-id: {self.account_id}")

        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{path}", headers=headers, timeout=15)

        if not r.is_success:
            logger.error(f"{r.status_code} {path} | {r.text[:300]}")

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
        g = conn.get("glucoseMeasurement", {})
        if not g:
            return None
        return {
            "value_mgdl": g.get("ValueInMgPerDl"),
            "timestamp": g.get("Timestamp"),
            "trend": g.get("TrendArrow"),
            "is_high": g.get("isHigh", False),
            "is_low": g.get("isLow", False),
        }

    async def get_graph(self) -> list[dict]:
        if not self.patient_id:
            await self.get_latest_reading()
        if not self.patient_id:
            return []
        data = await self._authed_get(f"/llu/connections/{self.patient_id}/graph")
        return [
            {
                "value_mgdl": i.get("ValueInMgPerDl"),
                "timestamp": i.get("Timestamp"),
                "trend": i.get("TrendArrow"),
                "is_high": False,
                "is_low": False,
            }
            for i in data.get("data", {}).get("graphData", [])
            if i.get("ValueInMgPerDl") is not None
        ]


libre_client = LibreClient()
