
import json
from pathlib import Path


# Путь к shops.json в корне проекта
SHOPS_FILE = Path(__file__).resolve().parent.parent / "shops.json"


def load_shops() -> list[dict]:
    """Загружает список магазинов из JSON."""

    if not SHOPS_FILE.exists():
        raise FileNotFoundError(
            f"Файл магазинов не найден: {SHOPS_FILE}"
        )

    with open(SHOPS_FILE, "r", encoding="utf-8") as file:
        shops = json.load(file)

    if not isinstance(shops, list):
        raise ValueError("shops.json должен содержать список магазинов")

    return shops


def get_shop_by_code(code: str) -> dict | None:
    """Ищет магазин по его номеру."""

    code = str(code).strip()

    shops = load_shops()

    for shop in shops:
        if str(shop.get("code", "")).strip() == code:
            return shop

    return None
