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

            # Loga resposta completa para diagnóstico
            logger.info(f"Login response: {json.dumps(data)[:800]}")

            if data.get("data", {}).get("redirect"):
                region = data["data"]["region"]
                BASE_URL = f"https://api-{region}.libreview.io"
                logger.info(f"Redirect to region: {region}")
                return await self.login(_redirect_count + 1)

            if data.get("status") == 2:
                await self._accept_terms()
                return await self.login(_redirect_count + 1)

            # authTicket pode estar em lugares diferentes
            auth = (
                data.get("data", {}).get("authTicket")
                or data.get("data", {}).get("AuthTicket")
                or data.get("authTicket")
            )
            if not auth:
                logger.error(f"authTicket não encontrado. data keys: {list(data.get('data', {}).keys())}")
                raise RuntimeError(f"authTicket não encontrado: {json.dumps(data)[:300]}")

            self.token = auth.get("token")
            jwt = decode_jwt(self.token)

            d = data.get("data", {})
            self.account_id = (
                d.get("accountId")
                or d.get("account_id")
                or jwt.get("accountId")
                or d.get("user", {}).get("accountId")
                or d.get("user", {}).get("id")
                or jwt.get("id")
            )

            logger.info(f"Login OK | role: {jwt.get('role')} | account_id: {self.account_id}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/llu/auth/continue/tou",
                headers=HEADERS, json={}, timeout=10,
            )

    async def _get(self, path: str, _retried: bool = False) -> dict:
        if not self.token:
            await self.login()

        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        if self.account_id:
            headers["account-id"] = self.account_id

        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{path}", headers=headers, timeout=15)

        logger.info(f"GET {path} → {r.status_code}")
        if not r.is_success:
            logger.error(f"Body: {r.text[:300]}")

        if r.status_code == 401 and not _retried:
            self.token = None
            await self.login()
            return await self._get(path, _retried=True)

        r.raise_for_status()
        return r.json()

    async def get_latest_reading(self) -> Optional[dict]:
        try:
            data = await self._get("/llu/connections")
            d = data.get("data", {})
            if isinstance(d, list) and d:
                conn = d[0]
                self.patient_id = conn.get("patientId")
                g = conn.get("glucoseMeasurement", {})
                if g:
                    return {
                        "value_mgdl": g.get("ValueInMgPerDl"),
                        "timestamp": g.get("Timestamp"),
                        "trend": g.get("TrendArrow"),
                        "is_high": g.get("isHigh", False),
                        "is_low": g.get("isLow", False),
                    }
            logger.warning(f"Conexões vazias ou formato inesperado: {json.dumps(data)[:300]}")
        except Exception as e:
            logger.warning(f"get_latest_reading falhou: {e}")
        return None

    async def get_graph(self) -> list[dict]:
        if not self.patient_id:
            await self.get_latest_reading()
        if not self.patient_id:
            return []
        try:
            data = await self._get(f"/llu/connections/{self.patient_id}/graph")
            return [
                {
                    "value_mgdl": i.get("ValueInMgPerDl"),
                    "timestamp": i.get("Timestamp"),
                    "trend": i.get("TrendArrow"),
                    "is_high": False,
                    "is_low": False,
                }
                for i in data.get("data", {}).get("graphData", [])
                if i.get("ValueInMgPerDl")
            ]
        except Exception as e:
            logger.warning(f"get_graph falhou: {e}")
            latest = await self.get_latest_reading()
            return [latest] if latest else []


libre_client = LibreClient()
