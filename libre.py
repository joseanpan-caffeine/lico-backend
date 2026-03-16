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
            logger.info(f"Login OK | role: {jwt.get('role')} | id: {self.account_id}")
            return True

    async def _accept_terms(self):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/llu/auth/continue/tou",
                headers=HEADERS, json={}, timeout=10,
            )

    async def _get(self, path: str, with_account_id: bool = True, _retried: bool = False) -> dict:
        if not self.token:
            await self.login()

        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        if with_account_id and self.account_id:
            headers["account-id"] = self.account_id

        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{path}", headers=headers, timeout=15)

        logger.info(f"GET {path} (account-id={'yes' if with_account_id else 'no'}) → {r.status_code}")
        if not r.is_success:
            logger.error(f"Body: {r.text[:300]}")

        if r.status_code == 401 and not _retried:
            self.token = None
            await self.login()
            return await self._get(path, with_account_id, _retried=True)

        r.raise_for_status()
        return r.json()

    async def _discover(self):
        """Testa todos os endpoints conhecidos para achar qual funciona."""
        endpoints = [
            ("/llu/connections", True),
            ("/llu/connections", False),
            (f"/llu/connections/{self.account_id}/graph", True),
            (f"/llu/connections/{self.account_id}/graph", False),
            ("/llu/graph", True),
            ("/llu/graph", False),
        ]
        for path, with_id in endpoints:
            try:
                data = await self._get(path, with_id)
                logger.info(f"SUCESSO em {path} (account-id={'yes' if with_id else 'no'}) | keys: {list(data.keys())}")
                logger.info(f"Resposta: {json.dumps(data)[:400]}")
                return data, path
            except Exception as e:
                logger.warning(f"Falhou {path}: {e}")
        return None, None

    async def get_latest_reading(self) -> Optional[dict]:
        data, path = await self._discover()
        if not data:
            return None

        # Tenta extrair leitura de diferentes estruturas
        d = data.get("data", {})

        # Estrutura de connections (caregiver)
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

        # Estrutura de graph
        graph = d.get("graphData", []) if isinstance(d, dict) else []
        if graph:
            latest = graph[-1]
            return {
                "value_mgdl": latest.get("ValueInMgPerDl"),
                "timestamp": latest.get("Timestamp"),
                "trend": latest.get("TrendArrow"),
                "is_high": False,
                "is_low": False,
            }

        logger.error(f"Estrutura desconhecida: {json.dumps(data)[:400]}")
        return None

    async def get_graph(self) -> list[dict]:
        if not self.patient_id:
            await self.get_latest_reading()

        # Tenta graph do patient_id descoberto
        if self.patient_id:
            try:
                data = await self._get(f"/llu/connections/{self.patient_id}/graph", True)
                items = data.get("data", {}).get("graphData", [])
                if items:
                    return [
                        {
                            "value_mgdl": i.get("ValueInMgPerDl"),
                            "timestamp": i.get("Timestamp"),
                            "trend": i.get("TrendArrow"),
                            "is_high": False,
                            "is_low": False,
                        }
                        for i in items if i.get("ValueInMgPerDl")
                    ]
            except Exception as e:
                logger.warning(f"graph falhou: {e}")

        latest = await self.get_latest_reading()
        return [latest] if latest else []


libre_client = LibreClient()
