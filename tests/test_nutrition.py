from __future__ import annotations

from weight_mcp.config import Settings
from weight_mcp.nutrition import NutritionLookup


def test_off_normalization(settings: Settings) -> None:
    lookup = NutritionLookup(settings)
    product: dict[str, object] = {
        "code": "123",
        "product_name": "Skyr",
        "brands": "Arla",
        "nutriments": {
            "energy-kcal_100g": 63,
            "proteins_100g": 11,
            "carbohydrates_100g": 4,
            "fat_100g": 0.2,
            "energy_100g": 264,  # kJ — must be ignored
        },
    }
    facts = lookup._off_to_facts(product)
    assert facts.source == "off"
    assert facts.kcal_per_100g == 63
    assert facts.protein_g_per_100g == 11
    assert facts.brand == "Arla"


def test_off_handles_missing_nutriments(settings: Settings) -> None:
    facts = NutritionLookup(settings)._off_to_facts({"product_name": "Mystery"})
    assert facts.name == "Mystery"
    assert facts.kcal_per_100g is None


def test_usda_normalization(settings: Settings) -> None:
    food: dict[str, object] = {
        "fdcId": 999,
        "description": "Chicken breast",
        "foodNutrients": [
            {"nutrientNumber": "208", "value": 165},
            {"nutrientNumber": "203", "value": 31},
            {"nutrientNumber": "204", "value": 3.6},
        ],
    }
    facts = NutritionLookup(settings)._usda_to_facts(food)
    assert facts.source == "usda"
    assert facts.kcal_per_100g == 165
    assert facts.protein_g_per_100g == 31
    assert facts.fat_g_per_100g == 3.6
