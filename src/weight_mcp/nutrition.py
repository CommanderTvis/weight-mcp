"""Lookups against public nutrition databases, normalized to per-100 g facts.

Open Food Facts is the primary source (no key, strong EU/German coverage).
USDA FoodData Central is an optional secondary source for generic foods (needs a
free key). External JSON is parsed into typed DTOs (below) before anything reads
it, so the rest of the module never touches a raw dict.
"""

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .models import NutritionFacts

_USER_AGENT = "weight-mcp/0.1 (https://github.com/commandertvis/weight-mcp)"
_OFF_BASE = "https://world.openfoodfacts.org"
_USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
_OFF_FIELDS = "code,product_name,brands,nutriments"


# --- Open Food Facts response DTOs ------------------------------------------


class OffNutriments(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # ``energy_100g`` is kilojoules, so it is deliberately not mapped here.
    kcal_per_100g: float | None = Field(default=None, alias="energy-kcal_100g")
    protein_g_per_100g: float | None = Field(default=None, alias="proteins_100g")
    carbs_g_per_100g: float | None = Field(default=None, alias="carbohydrates_100g")
    fat_g_per_100g: float | None = Field(default=None, alias="fat_100g")
    fiber_g_per_100g: float | None = Field(default=None, alias="fiber_100g")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class OffProduct(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    product_name: str | None = None
    brands: str | None = None
    nutriments: OffNutriments = Field(default_factory=OffNutriments)


class OffProductResponse(BaseModel):
    status: int = 0
    product: OffProduct | None = None


class OffSearchResponse(BaseModel):
    products: list[OffProduct] = Field(default_factory=list)


# --- USDA FoodData Central response DTOs ------------------------------------


class UsdaNutrient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: str | None = Field(default=None, alias="nutrientNumber")
    value: float | None = None


class UsdaFood(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    fdc_id: int | None = Field(default=None, alias="fdcId")
    description: str | None = None
    brand_owner: str | None = Field(default=None, alias="brandOwner")
    nutrients: list[UsdaNutrient] = Field(default_factory=list, alias="foodNutrients")


class UsdaSearchResponse(BaseModel):
    foods: list[UsdaFood] = Field(default_factory=list)


def off_to_facts(product: OffProduct) -> NutritionFacts:
    n = product.nutriments
    return NutritionFacts(
        name=product.product_name or "Unknown product",
        brand=product.brands or None,
        source="off",
        source_id=product.code,
        kcal_per_100g=n.kcal_per_100g,
        protein_g_per_100g=n.protein_g_per_100g,
        carbs_g_per_100g=n.carbs_g_per_100g,
        fat_g_per_100g=n.fat_g_per_100g,
        fiber_g_per_100g=n.fiber_g_per_100g,
    )


def usda_to_facts(food: UsdaFood) -> NutritionFacts:
    by_number = {n.number: n.value for n in food.nutrients if n.number}
    return NutritionFacts(
        name=food.description or "Unknown food",
        brand=food.brand_owner or None,
        source="usda",
        source_id=str(food.fdc_id) if food.fdc_id is not None else None,
        kcal_per_100g=by_number.get("208"),
        protein_g_per_100g=by_number.get("203"),
        carbs_g_per_100g=by_number.get("205"),
        fat_g_per_100g=by_number.get("204"),
        fiber_g_per_100g=by_number.get("291"),
    )


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
        parsed = OffProductResponse.model_validate(resp.json())
        if parsed.status != 1 or parsed.product is None:
            return None
        return off_to_facts(parsed.product)

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
        parsed = OffSearchResponse.model_validate(resp.json())
        return [off_to_facts(p) for p in parsed.products[:limit]]

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
        parsed = UsdaSearchResponse.model_validate(resp.json())
        return [usda_to_facts(f) for f in parsed.foods[:limit]]
