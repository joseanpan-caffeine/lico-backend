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


def decode_jwt_payload(token: str) -> dict:
    """Decodifica o payload do JWT sem verificar assinatura."""
    try:
        payload_b64 = token.split(".")[1]
        # JWT usa base64url — adiciona padding se necessário
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logger.error(f"Erro ao decodificar JWT: {e}")
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

            self.token = data.get("data", {}).get("authTicket", {}).get("token")
            if not self.token:
                raise RuntimeError(f"Token não encontrado: {data}")

            # Decodifica o JWT para ver o account-id que a Abbott espera
            jwt_payload = decode_jwt_payload(self.token)
            logger.info(f"JWT payload keys: {list(jwt_payload.keys())}")
            logger.info(f"JWT payload: {jwt_payload}")

            # O account-id correto vem do JWT, não do user object
            self.account_id = (
                jwt_payload.get("id")
                or jwt_payload.get("accountId")
                or jwt_payload.get("sub")
                or data.get("data", {}).get("user", {}).get("id")
            )

            logger.info(f"Login OK | account_id: {self.account_id}")
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
        if self.account_id:
            # Envia sem hífens
            headers["account-id"] = self.account_id.replace("-", "")
            logger.info(f"account-id enviado: {headers['account-id']}")
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
