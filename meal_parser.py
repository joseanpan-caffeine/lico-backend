"""
Endpoint POST /meals/parse
Recebe descrição em texto livre → retorna itens estruturados mapeados ao foods.json
"""
import json
import os
import re
import unicodedata
from pathlib import Path
import httpx

_FOODS_PATH = Path(__file__).parent / "foods.json"
_all_foods: list[dict] = json.loads(_FOODS_PATH.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    """Remove acentos e normaliza para minúsculas."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


# Índice com tokens normalizados (sem acentos)
_food_tokens: list[tuple[set[str], dict]] = []
for food in _all_foods:
    tokens = set(re.findall(r'\b\w{3,}\b', _normalize(food["nome"])))
    _food_tokens.append((tokens, food))


def _candidate_foods(description: str, max_candidates: int = 60) -> list[dict]:
    """Filtra foods.json pelas palavras da descrição — normalizado sem acentos."""
    words = set(re.findall(r'\b\w{3,}\b', _normalize(description)))

    scored = []
    for tokens, food in _food_tokens:
        overlap = len(words & tokens)
        if overlap > 0:
            scored.append((overlap, food))

    scored.sort(key=lambda x: -x[0])
    candidates = [f for _, f in scored[:max_candidates]]

    # Se poucos candidatos, inclui alimentos comuns como fallback
    if len(candidates) < 10:
        fallback_names = ["arroz", "feijão", "pão", "frango", "carne", "leite", "ovo", "maçã", "banana", "batata"]
        for fn in fallback_names:
            fn_norm = _normalize(fn)
            for tokens, food in _food_tokens:
                if fn_norm in _normalize(food["nome"]) and food not in candidates:
                    candidates.append(food)
                    break

    return candidates[:max_candidates]


SYSTEM_PROMPT = """Você é um assistente de contagem de carboidratos para diabetes.
O usuário descreve o que comeu. Sua tarefa é mapear cada alimento para o item correto do banco de dados fornecido.

REGRAS OBRIGATÓRIAS:
1. SEMPRE use cho_unit_g exatamente como está no banco de dados — NUNCA estime ou calcule
2. SEMPRE use o campo "cho_g" do candidato como cho_unit_g na resposta
3. Se encontrar o alimento no banco, matched_from_db = true e cho_unit_g = cho_g do banco
4. Se NÃO encontrar, escolha o candidato mais similar e use seu cho_g — matched_from_db = false
5. NUNCA invente valores de carboidrato

Quantidades:
- Sem quantidade mencionada → quantity: 1.0
- "dois", "2" → quantity: 2.0
- "meia", "metade" → quantity: 0.5
- "um prato" de arroz → quantity: 4 (4 colheres de sopa)
- "um copo" de leite → quantity: 1

Retorne SOMENTE JSON válido, sem texto adicional, sem markdown:
[
  {
    "food_name": "nome exato do banco",
    "medida": "medida caseira do banco",
    "quantity": 1.0,
    "cho_unit_g": 12.0,
    "kcal": 47.0,
    "grupo": "grupo do banco",
    "matched_from_db": true
  }
]"""


async def parse_meal_text(description: str) -> list[dict]:
    """Chama Claude API para parsear descrição em itens estruturados."""
    candidates = _candidate_foods(description)

    candidates_text = json.dumps(
        [{"nome": f["nome"], "medida": f["medida"], "cho_g": f["cho_g"],
          "peso_g": f.get("peso_g"), "kcal": f.get("kcal"), "grupo": f["grupo"]}
         for f in candidates],
        ensure_ascii=False, indent=None
    )

    user_message = f"""Refeição: "{description}"

Banco de dados (use cho_g como cho_unit_g na resposta):
{candidates_text}

JSON:"""

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
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        r.raise_for_status()
        data = r.json()

    raw = data["content"][0]["text"].strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    items = json.loads(raw)
    return items if isinstance(items, list) else []
