"""
Meal parser — arquitetura limpa:
- Claude: identifica nome do alimento + quantidade + unidade informada pelo usuário
- Python: busca no banco, faz toda a conversão de CHO
"""
import json, os, re, unicodedata
from pathlib import Path
import httpx

_FOODS_PATH = Path(__file__).parent / "foods.json"
_all_foods: list[dict] = json.loads(_FOODS_PATH.read_text(encoding="utf-8"))

def _normalize(text: str) -> str:
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()

_food_tokens: list[tuple[set[str], dict]] = []
for food in _all_foods:
    tokens = set(re.findall(r'\b\w{3,}\b', _normalize(food["nome"])))
    _food_tokens.append((tokens, food))

def _find_best_food(name: str) -> dict | None:
    """Busca o alimento no banco, priorizando CDBH."""
    name_norm = _normalize(name)
    name_words = set(re.findall(r'\b\w{3,}\b', name_norm))

    scored = []
    for tokens, food in _food_tokens:
        overlap = len(name_words & tokens)
        if overlap > 0:
            # CDBH tem prioridade — penaliza outras fontes
            fonte_penalty = 0 if food.get("fonte") == "CDBH-MANUAL" else 1
            scored.append((overlap, fonte_penalty, food))

    if not scored:
        return None

    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]

def _candidate_foods(description: str, max_candidates: int = 40) -> list[dict]:
    words = set(re.findall(r'\b\w{3,}\b', _normalize(description)))
    scored = []
    for tokens, food in _food_tokens:
        overlap = len(words & tokens)
        if overlap > 0:
            fonte_penalty = 0 if food.get("fonte") == "CDBH-MANUAL" else 1
            scored.append((overlap, fonte_penalty, food))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [f for _, _, f in scored[:max_candidates]]

def _convert_cho(food: dict, quantity: float, unit: str) -> tuple[float | None, str | None]:
    """
    Converte quantidade do usuário para CHO total.
    Retorna (cho_total, error_msg).
    """
    cho_unit = food["cho_g"]
    peso_g = food.get("peso_g")
    medida = food["medida"]
    unit_n = _normalize(unit)

    # Peso em gramas
    if any(u in unit_n for u in ["grama", " g", "kg"]):
        if not peso_g:
            return None, f'Não é possível converter "{food["nome"]}" por peso. Informe em: {medida}'
        multiplier = 1000.0 if "kg" in unit_n else 1.0
        cho = (quantity * multiplier / peso_g) * cho_unit
        return round(cho, 1), None

    # Unidade (banana, ovo, pão)
    if any(u in unit_n for u in ["unidade", "uni", "inteiro", "inteira", "peca", "peça"]):
        medida_n = _normalize(medida)
        if any(u in medida_n for u in ["unidade", "uni", "inteiro", "peca"]):
            return round(quantity * cho_unit, 1), None
        return None, f'"{food["nome"]}" não é medido por unidade. Informe em: {medida}'

    # Medidas caseiras genéricas — aceita se o banco usa medida similar
    medida_n = _normalize(medida)
    for kw in ["colher", "xicara", "copo", "fatia", "porcao", "concha", "pegador", "prato", "sache", "barra"]:
        if kw in unit_n and kw in medida_n:
            return round(quantity * cho_unit, 1), None

    # Sem unidade ou "porção" → 1 medida do banco
    if unit_n in ["", "porcao", "porcoes", "dose", "porc"]:
        return round(quantity * cho_unit, 1), None

    return None, f'Não consigo converter "{unit}" para "{food["nome"]}". Informe em: {medida}'


SYSTEM_PROMPT = """Você identifica alimentos em descrições de refeições em português brasileiro.

Para cada alimento mencionado, retorne:
- food_name: nome do alimento em português, simples e genérico (ex: "arroz branco cozido", "banana prata", "feijão carioca cozido")
- quantity: número (float) — a quantidade informada pelo usuário
- unit: a unidade informada pelo usuário

UNIDADES:
- "gramas" — quando disse "g", "gramas", "100g", "200 g"
- "unidade" — quando disse "unidade", "1 banana", "2 ovos", "um pão"
- "colher" — quando disse "colher de sopa", "colheres"
- "xicara" — xícara
- "copo" — copo
- "concha" — concha
- "porcao" — quando não informou unidade, ou disse "porção"

QUANTIDADES padrão quando não informadas:
- Frutas → quantity: 1, unit: "unidade"
- Arroz, feijão, legumes → quantity: 1, unit: "porcao"
- Pão → quantity: 1, unit: "unidade"
- Ovo → quantity: 1, unit: "unidade"

Retorne SOMENTE JSON, sem markdown:
[{"food_name": "arroz branco cozido", "quantity": 4.0, "unit": "colher"}]"""


async def parse_meal_text(description: str) -> list[dict]:
    candidates = _candidate_foods(description)
    candidates_text = json.dumps(
        [{"nome": f["nome"], "medida": f["medida"], "fonte": f.get("fonte","")}
         for f in candidates],
        ensure_ascii=False
    )

    user_message = f'Refeição: "{description}"\nAlimentos disponíveis no banco: {candidates_text}\nJSON:'

    api_key = os.getenv("ANTHROPIC_API_KEY")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        r.raise_for_status()
        data = r.json()

    raw = data["content"][0]["text"].strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    claude_items = json.loads(raw)
    if not isinstance(claude_items, list):
        return []

    # Python faz TODA a busca e conversão
    result = []
    for item in claude_items:
        food_name = item.get("food_name", "")
        quantity = float(item.get("quantity", 1.0))
        unit = item.get("unit", "porcao")

        food = _find_best_food(food_name)

        if not food:
            result.append({
                "food_name": food_name,
                "medida": None,
                "quantity": quantity,
                "cho_unit_g": 0,
                "cho_total_g": 0,
                "kcal": None,
                "grupo": None,
                "matched_from_db": False,
                "fonte": None,
                "conversion_error": f'"{food_name}" não encontrado na tabela nutricional.'
            })
            continue

        cho_total, error = _convert_cho(food, quantity, unit)

        result.append({
            "food_name": food["nome"],
            "medida": food["medida"],
            "quantity": quantity,
            "unit_informed": unit,
            "cho_unit_g": food["cho_g"],
            "cho_total_g": cho_total if cho_total is not None else 0,
            "kcal": food.get("kcal"),
            "grupo": food.get("grupo"),
            "matched_from_db": True,
            "fonte": food.get("fonte"),
            "conversion_error": error
        })

    return result
