from weight_mcp.nutrition import (
    OffProduct,
    UsdaFood,
    off_to_facts,
    usda_to_facts,
)


def test_off_normalization() -> None:
    product = OffProduct.model_validate(
        {
            "code": "123",
            "product_name": "Skyr",
            "brands": "Arla",
            "nutriments": {
                "energy-kcal_100g": 63,
                "proteins_100g": 11,
                "carbohydrates_100g": 4,
                "fat_100g": 0.2,
                "fiber_100g": 1.5,
                "energy_100g": 264,  # kJ — must be ignored
            },
        }
    )
    facts = off_to_facts(product)
    assert facts.source == "off"
    assert facts.kcal_per_100g == 63
    assert facts.protein_g_per_100g == 11
    assert facts.fiber_g_per_100g == 1.5
    assert facts.brand == "Arla"


def test_off_handles_missing_nutriments() -> None:
    facts = off_to_facts(OffProduct.model_validate({"product_name": "Mystery"}))
    assert facts.name == "Mystery"
    assert facts.kcal_per_100g is None


def test_off_blank_nutriment_is_none() -> None:
    product = OffProduct.model_validate(
        {"product_name": "Water", "nutriments": {"energy-kcal_100g": "", "proteins_100g": 0}}
    )
    facts = off_to_facts(product)
    assert facts.kcal_per_100g is None
    assert facts.protein_g_per_100g == 0


def test_usda_normalization() -> None:
    food = UsdaFood.model_validate(
        {
            "fdcId": 999,
            "description": "Chicken breast",
            "foodNutrients": [
                {"nutrientNumber": "208", "value": 165},
                {"nutrientNumber": "203", "value": 31},
                {"nutrientNumber": "204", "value": 3.6},
                {"nutrientNumber": "291", "value": 0.5},
            ],
        }
    )
    facts = usda_to_facts(food)
    assert facts.source == "usda"
    assert facts.kcal_per_100g == 165
    assert facts.protein_g_per_100g == 31
    assert facts.fat_g_per_100g == 3.6
    assert facts.fiber_g_per_100g == 0.5
    assert facts.source_id == "999"
