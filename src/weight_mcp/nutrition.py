"""Lookups against public nutrition databases, normalized to per-100 g facts.

Open Food Facts is the primary source (no key, strong EU/German coverage).
USDA FoodData Central is an optional secondary source for generic foods (needs a
free key). Everything is normalized to :class:`~weight_mcp.models.NutritionFacts`.
"""

from __future__ import annotations

import httpx

from .config import Settings
from .models import NutritionFacts

_USER_AGENT = "weight-mcp/0.1 (https://github.com/commandertvis/weight-mcp)"
_OFF_BASE = "https://world.openfoodfacts.org"
_USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
_OFF_FIELDS = "code,product_name,brands,nutriments"


def _f(value: object) -> float | None:
    """Coerce a JSON number to float, tolerating strings and missing values."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class NutritionLookup:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": _USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, limit: int = 5) -> list[NutritionFacts]:
        """Text search across the configured sources, OFF first, USDA as fallback."""
        results: list[NutritionFacts] = []
        if self._settings.off_enabled:
            results.extend(await self._off_search(query, limit=limit))
        if len(results) < limit and self._settings.usda_enabled and self._settings.usda_api_key:
            results.extend(await self._usda_search(query, limit=limit - len(results)))
        return results

    async def by_barcode(self, barcode: str) -> NutritionFacts | None:
        """Look up a single product by barcode (Open Food Facts only)."""
        if not self._settings.off_enabled:
            return None
        resp = await self._client.get(
            f"{_OFF_BASE}/api/v2/product/{barcode}.json",
            params={"fields": _OFF_FIELDS},
        )
        if resp.status_code != httpx.codes.OK:
            return None
        data = resp.json()
        if data.get("status") != 1:
            return None
        return self._off_to_facts(data.get("product", {}))

    # --- Open Food Facts ----------------------------------------------------

    async def _off_search(self, query: str, *, limit: int) -> list[NutritionFacts]:
        resp = await self._client.get(
            f"{_OFF_BASE}/cgi/search.pl",
            params={
                "search_terms": query,
                "search_simple": "1",
                "json": "1",
                "page_size": str(limit),
                "fields": _OFF_FIELDS,
                "countries_tags_en": self._settings.off_country,
            },
        )
        if resp.status_code != httpx.codes.OK:
            return []
        products = resp.json().get("products", [])
        return [self._off_to_facts(p) for p in products[:limit]]

    def _off_to_facts(self, product: dict[str, object]) -> NutritionFacts:
        nutriments = product.get("nutriments", {})
        if not isinstance(nutriments, dict):
            nutriments = {}
        return NutritionFacts(
            name=str(product.get("product_name") or "Unknown product"),
            brand=str(product["brands"]) if product.get("brands") else None,
            source="off",
            source_id=str(product["code"]) if product.get("code") else None,
            kcal_per_100g=_f(nutriments.get("energy-kcal_100g")),
            protein_g_per_100g=_f(nutriments.get("proteins_100g")),
            carbs_g_per_100g=_f(nutriments.get("carbohydrates_100g")),
            fat_g_per_100g=_f(nutriments.get("fat_100g")),
        )

    # --- USDA FoodData Central ---------------------------------------------

    async def _usda_search(self, query: str, *, limit: int) -> list[NutritionFacts]:
        resp = await self._client.get(
            f"{_USDA_BASE}/foods/search",
            params={
                "query": query,
                "pageSize": str(limit),
                "api_key": self._settings.usda_api_key or "",
            },
        )
        if resp.status_code != httpx.codes.OK:
            return []
        foods = resp.json().get("foods", [])
        return [self._usda_to_facts(f) for f in foods[:limit]]

    def _usda_to_facts(self, food: dict[str, object]) -> NutritionFacts:
        by_number: dict[str, float | None] = {}
        nutrients = food.get("foodNutrients", [])
        if isinstance(nutrients, list):
            for n in nutrients:
                if isinstance(n, dict):
                    number = str(n.get("nutrientNumber", ""))
                    by_number[number] = _f(n.get("value"))
        return NutritionFacts(
            name=str(food.get("description") or "Unknown food"),
            brand=str(food["brandOwner"]) if food.get("brandOwner") else None,
            source="usda",
            source_id=str(food["fdcId"]) if food.get("fdcId") else None,
            kcal_per_100g=by_number.get("208"),
            protein_g_per_100g=by_number.get("203"),
            carbs_g_per_100g=by_number.get("205"),
            fat_g_per_100g=by_number.get("204"),
        )
