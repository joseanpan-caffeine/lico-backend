"""
Endpoint POST /meals/parse
Recebe descrição em texto livre → retorna itens estruturados mapeados ao foods.json
"""
import json
import os
import re
from pathlib import Path
from typing import Optional
import httpx

_FOODS_PATH = Path(__file__).parent / "foods.json"
_all_foods: list[dict] = json.loads(_FOODS_PATH.read_text(encoding="utf-8"))

# Índice enriquecido: palavras tokenizadas por alimento para pré-filtro rápido
_food_tokens: list[tuple[set[str], dict]] = []
for i, food in enumerate(_all_foods):
    tokens = set(re.findall(r'\b\w{3,}\b', food["nome"].lower()))
    _food_tokens.append((tokens, {**food, "_id": i}))


def _candidate_foods(description: str, max_candidates: int = 60) -> list[dict]:
    """Filtra foods.json pelas palavras da descrição antes de chamar a LLM."""
    words = set(re.findall(r'\b\w{3,}\b', description.lower()))
    
    scored = []
    for tokens, food in _food_tokens:
        overlap = len(words & tokens)
        if overlap > 0:
            scored.append((overlap, food))
    
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:max_candidates]]


SYSTEM_PROMPT = """Você é um assistente de contagem de carboidratos para uma pessoa com diabetes.
O usuário descreve em linguagem natural o que comeu. Sua tarefa:

1. Identificar cada alimento/ingrediente mencionado na descrição
2. Mapear para o item mais adequado da lista de candidatos fornecida
3. Estimar a quantidade consumida (em múltiplos da medida caseira do item)
4. Retornar SOMENTE um JSON válido, sem texto adicional, sem markdown

Regras:
- Prefira sempre o item mais simples/genérico (ex: "Arroz branco cozido" > "Arroz à grega")
- Se a quantidade não for mencionada, assuma 1.0
- Se não encontrar match adequado nos candidatos, use cho_g estimado razoável e marque matched_from_db: false
- Números por extenso devem ser convertidos (dois → 2, meia → 0.5)
- "prato" de arroz = ~4 colheres de sopa = quantity: 4
- "copo" de suco = 1 unidade

Formato de resposta (array JSON):
[
  {
    "food_name": "nome do alimento identificado",
    "medida": "medida caseira da porção base",
    "quantity": 1.0,
    "cho_unit_g": 28.0,
    "kcal": 135.0,
    "grupo": "grupo alimentar",
    "matched_from_db": true
  }
]"""


async def parse_meal_text(description: str) -> list[dict]:
    """Chama Claude API para parsear descrição em itens estruturados."""
    candidates = _candidate_foods(description)
    
    candidates_text = json.dumps(
        [{"nome": f["nome"], "medida": f["medida"], "cho_g": f["cho_g"],
          "kcal": f.get("kcal"), "grupo": f["grupo"]} for f in candidates],
        ensure_ascii=False, indent=None
    )
    
    user_message = f"""Descrição da refeição: "{description}"

Candidatos do banco de dados:
{candidates_text}

Retorne SOMENTE o JSON com os itens identificados."""

    api_key = os.getenv("ANTHROPIC_API_KEY")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",  # rápido e barato para parsing
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        r.raise_for_status()
        data = r.json()
    
    raw = data["content"][0]["text"].strip()
    # Remove markdown fences se presentes
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    
    items = json.loads(raw)
    return items if isinstance(items, list) else []
