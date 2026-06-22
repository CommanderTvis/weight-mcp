"""Typed domain models."""

from datetime import date, datetime

from pydantic import BaseModel


class WeightEntry(BaseModel):
    id: int
    recorded_at: datetime
    weight_kg: float


class FoodLog(BaseModel):
    """One logged food item. A "meal" is one or more of these eaten together."""

    id: int
    eaten_at: datetime
    name: str
    quantity_g: float | None
    kcal: float
    protein_g: float
    carbs_g: float | None
    fat_g: float | None
    source: str | None  # e.g. "off", "usda", "manual"


class NutritionFacts(BaseModel):
    """A normalized hit from a public nutrition database (per 100 g)."""

    name: str
    brand: str | None
    source: str
    source_id: str | None
    kcal_per_100g: float | None
    protein_g_per_100g: float | None
    carbs_g_per_100g: float | None
    fat_g_per_100g: float | None


class DayTotals(BaseModel):
    day: date
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    item_count: int


class Progress(BaseModel):
    """Today's intake against the configured goal."""

    day: date
    goal_mode: str
    kcal: float
    kcal_target: int
    protein_g: float
    protein_target_g: int

    @property
    def kcal_remaining(self) -> float:
        return self.kcal_target - self.kcal

    @property
    def protein_remaining_g(self) -> float:
        return self.protein_target_g - self.protein_g
