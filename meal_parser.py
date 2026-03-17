"""
Endpoint POST /meals/parse
Arquitetura:
- Claude identifica: qual alimento, quantidade e unidade informada pelo usuário
- Python converte para CHO usando o banco de dados
- Se conversão impossível, retorna erro com instrução clara
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
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


_food_tokens: list[tuple[set[str], dict]] = []
for food in _all_foods:
    tokens = set(re.findall(r'\b\w{3,}\b', _normalize(food["nome"])))
    _food_tokens.append((tokens, food))


def _candidate_foods(description: str, max_candidates: int = 60) -> list[dict]:
    words = set(re.findall(r'\b\w{3,}\b', _normalize(description)))
    scored = []
    for tokens, food in _food_tokens:
        overlap = len(words & tokens)
        if overlap > 0:
            scored.append((overlap, food))
    scored.sort(key=lambda x: (-x[0], 0 if x[1].get("fonte") == "CDBH-MANUAL" else 1))
    candidates = [f for _, f in scored[:max_candidates]]

    if len(candidates) < 10:
        fallback_names = ["arroz", "feijão", "pão", "frango", "carne", "leite", "ovo", "maçã", "banana", "batata"]
        for fn in fallback_names:
            fn_norm = _normalize(fn)
            for tokens, food in _food_tokens:
                if fn_norm in _normalize(food["nome"]) and food not in candidates:
                    candidates.append(food)
                    break

    return candidates[:max_candidates]


# Unidades que o usuário pode informar e como se relacionam com peso
UNIT_TO_GRAMS = {
    "g": 1.0,
    "grama": 1.0,
    "gramas": 1.0,
    "kg": 1000.0,
    "ml": 1.0,  # aproximação para líquidos
}

# Unidades de medida caseira — Claude vai retornar o tipo
MEDIDA_TYPES = ["gramas", "unidade", "colher", "xicara", "copo", "fatia", "porcao", "concha", "pegador", "prato"]


def _convert_to_cho(food: dict, user_quantity: float, user_unit: str) -> tuple[float, str]:
    """
    Converte quantidade do usuário para CHO.
    Retorna (cho_total, erro_msg).
    erro_msg é None se conversão ok, string com instrução se não conseguir.
    """
    nome = food["nome"]
    medida_banco = food["medida"]  # ex: "1 colher de sopa cheia", "1 unidade", "100g"
    cho_unit = food["cho_g"]       # CHO para 1 medida do banco
    peso_g = food.get("peso_g")    # peso em gramas de 1 medida do banco

    unit_norm = _normalize(user_unit)

    # Caso 1: usuário informou em gramas/peso
    if any(u in unit_norm for u in ["g", "grama", "kg", "ml"]):
        if not peso_g or peso_g == 0:
            return 0, f'Não é possível converter "{nome}" por peso. Informe em: {medida_banco}'
        multiplier = 1000.0 if "kg" in unit_norm else 1.0
        user_grams = user_quantity * multiplier
        cho = (user_grams / peso_g) * cho_unit
        return round(cho, 1), None

    # Caso 2: usuário informou em unidades e banco também é por unidade
    if any(u in unit_norm for u in ["unidade", "uni", "unid", "inteiro", "inteira", "peça", "peca"]):
        medida_norm = _normalize(medida_banco)
        if any(u in medida_norm for u in ["unidade", "uni", "inteiro", "peca", "peça"]):
            cho = user_quantity * cho_unit
            return round(cho, 1), None
        # banco não é por unidade
        return 0, f'"{nome}" não é medido por unidade. Informe em: {medida_banco}'

    # Caso 3: usuário informou colher, xícara, copo etc — verifica se banco usa medida similar
    medida_norm = _normalize(medida_banco)
    for keyword in ["colher", "xicara", "copo", "fatia", "porcao", "concha", "pegador", "prato", "sache", "barra", "fatia"]:
        if keyword in unit_norm and keyword in medida_norm:
            cho = user_quantity * cho_unit
            return round(cho, 1), None

    # Caso 4: usuário não informou unidade (quantidade padrão = 1 medida do banco)
    if unit_norm in ["", "porcao", "porcoes", "dose"]:
        cho = user_quantity * cho_unit
        return round(cho, 1), None

    # Caso 5: unidade incompatível
    return 0, f'Não consigo converter "{user_unit}" para "{nome}". Informe em: {medida_banco}'


SYSTEM_PROMPT = """Você é um assistente de identificação de alimentos para contagem de carboidratos.

Sua única função é identificar:
1. Qual alimento do banco de dados melhor corresponde ao que o usuário descreveu
2. A quantidade numérica informada pelo usuário
3. A unidade de medida informada pelo usuário

## REGRAS

- PRIORIDADE DE FONTE: Sempre prefira itens com fonte "CDBH-MANUAL" sobre "TACO-UNICAMP" ou "TBCA-IG-USP"
- Itens CDBH têm medidas caseiras (colher, unidade, fatia). Itens TACO têm medida "100g"
- Quando o usuário informa peso (ex: "100g de arroz"), escolha o item CDBH mesmo assim — o sistema converte o peso automaticamente
- Escolha SEMPRE o item mais simples e genérico do banco (ex: "Arroz branco cozido" > versão específica)
- Se não houver quantidade, use quantity: 1.0 e unit: "porcao"
- Se não houver unidade mas houver quantidade numérica, infira a unidade pelo contexto
- Para frutas sem quantidade → quantity: 1.0, unit: "unidade"
- Para arroz/feijão/legumes sem quantidade → quantity: 1.0, unit: "porcao"
- Números por extenso: "dois" → 2.0, "meia" → 0.5, "três" → 3.0

## UNIDADES VÁLIDAS para o campo "unit":
- "gramas" — quando usuário disse "g", "gramas", "100g"
- "unidade" — quando usuário disse "unidade", "1 banana", "2 ovos"
- "colher" — quando disse "colher de sopa", "colher"
- "xicara" — quando disse "xícara", "xicara"
- "copo" — quando disse "copo"
- "fatia" — quando disse "fatia"
- "concha" — quando disse "concha"
- "porcao" — quando não informou unidade ou disse "porção"

## FORMATO DE RESPOSTA
JSON array, sem markdown:
[
  {
    "food_name": "nome exato do banco",
    "db_food": { objeto completo do banco },
    "quantity": 1.0,
    "unit": "unidade",
    "matched_from_db": true
  }
]

Se não encontrar no banco: matched_from_db: false, db_food: null"""


async def parse_meal_text(description: str) -> list[dict]:
    candidates = _candidate_foods(description)

    candidates_text = json.dumps(
        [{"nome": f["nome"], "medida": f["medida"], "cho_g": f["cho_g"],
          "peso_g": f.get("peso_g"), "kcal": f.get("kcal"), "grupo": f["grupo"], "fonte": f.get("fonte","")}
         for f in candidates],
        ensure_ascii=False, indent=None
    )

    user_message = f"""Descrição: "{description}"

Banco disponível:
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

    claude_items = json.loads(raw)
    if not isinstance(claude_items, list):
        return []

    # Python faz a conversão para CHO
    result = []
    for item in claude_items:
        if not item.get("matched_from_db") or not item.get("db_food"):
            # Não encontrado no banco — bloqueia cálculo
            result.append({
                "food_name": item.get("food_name", "Alimento desconhecido"),
                "medida": None,
                "quantity": item.get("quantity", 1.0),
                "cho_unit_g": 0,
                "kcal": None,
                "grupo": None,
                "matched_from_db": False,
                "conversion_error": f'"{item.get("food_name")}" não encontrado na tabela nutricional. Verifique o nome do alimento.'
            })
            continue

        db_food = item["db_food"]
        # Busca o food real no banco pelo nome (Claude pode ter alterado levemente)
        food_match = next(
            (f for f in _all_foods if _normalize(f["nome"]) == _normalize(db_food.get("nome", ""))),
            None
        )
        if not food_match:
            # Fallback: usa o db_food do Claude diretamente
            food_match = db_food

        quantity = float(item.get("quantity", 1.0))
        unit = item.get("unit", "porcao")

        cho_total, error = _convert_to_cho(food_match, quantity, unit)

        if error:
            result.append({
                "food_name": item.get("food_name"),
                "medida": food_match.get("medida"),
                "quantity": quantity,
                "cho_unit_g": food_match.get("cho_g", 0),
                "kcal": food_match.get("kcal"),
                "grupo": food_match.get("grupo"),
                "matched_from_db": True,
                "conversion_error": error
            })
        else:
            result.append({
                "food_name": item.get("food_name"),
                "medida": food_match.get("medida"),
                "quantity": quantity,
                "unit_informed": unit,
                "cho_unit_g": food_match.get("cho_g", 0),
                "cho_total_g": cho_total,
                "kcal": food_match.get("kcal"),
                "grupo": food_match.get("grupo"),
                "matched_from_db": True,
                "conversion_error": None
            })

    return result
