"""Проставляет lat/lon в shops.json по адресам (OpenStreetMap Nominatim).

Запускать на своём компьютере с интернетом из корня проекта:
    python map_panel/geocode_shops.py

Магазины, у которых координаты уже есть, пропускаются, поэтому скрипт можно
запускать повторно. Для каждого адреса пробуем по очереди: дом -> улица -> центр
населённого пункта. Точность печатается в строке результата. Всё, что найдено
«по улице» или «по городу», проверьте на карте и при необходимости поправьте
вручную (правый клик по зданию в Яндекс/Google/2ГИС -> скопировать координаты).
"""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SHOPS = Path(__file__).resolve().parent.parent / "shops.json"
URL = "https://nominatim.openstreetmap.org/search?format=json&limit=1&bounded=1&viewbox=32.4,46.3,36.7,44.3&q="
HEADERS = {"User-Agent": "emergency-service-map/1.0 (internal tool)"}
STREET_TYPE = re.compile(r"^(?:ул|улица|пр-т|пр|просп|проспект|бр-р|бул|пер|переулок)(?:\.\s*|\s+)", re.I)


def parse(address: str):
    """'г. Симферополь, ул. Русская 38а' -> ('Симферополь', 'Русская', '38а')."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    city = re.sub(r"^(г|пгт|с|пос|п)\.?\s+", "", parts[0], flags=re.I)
    rest = " ".join(parts[1:])
    rest = re.sub(r"\bд\.?\s*(?=\d)", "", rest)  # 'д. 12' -> '12'
    m = re.match(r"^(.*?)[\s,]+(\d[\d/\-]*\s?[A-Za-zА-Яа-я]?)$", rest)
    street, house = (m.group(1), m.group(2).replace(" ", "")) if m else (rest, "")
    return city, STREET_TYPE.sub("", street.strip()), house


def query(text: str):
    req = urllib.request.Request(URL + urllib.parse.quote(text), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    time.sleep(1.2)  # правило Nominatim: не чаще 1 запроса в секунду
    return (float(data[0]["lat"]), float(data[0]["lon"])) if data else None


def geocode(address: str):
    city, street, house = parse(address)
    attempts = []
    if street and house:
        attempts.append((f"{street} {house}, {city}", "дом"))
    if street:
        attempts.append((f"{street}, {city}", "УЛИЦА (примерно)"))
    attempts.append((city, "ГОРОД (очень примерно)"))
    for text, precision in attempts:
        try:
            point = query(text)
        except Exception as exc:
            print(f"   ошибка запроса «{text}»: {exc}")
            continue
        if point:
            return point, precision
    return None, None


def main() -> None:
    shops = json.loads(SHOPS.read_text(encoding="utf-8"))
    rough = []
    for shop in shops:
        if shop.get("lat") is not None and shop.get("lon") is not None:
            continue
        point, precision = geocode(shop["address"])
        if point:
            shop["lat"], shop["lon"] = round(point[0], 6), round(point[1], 6)
            if precision != "дом":
                rough.append(shop["code"])
            print(f"{shop['code']}: {shop['lat']}, {shop['lon']}  [{precision}]  {shop['address'].strip()}")
        else:
            rough.append(shop["code"])
            print(f"{shop['code']}: НЕ НАЙДЕН  {shop['address'].strip()}")

    SHOPS.write_text(json.dumps(shops, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"\nГотово. Проверьте вручную: {rough or 'все точки по дому'}")


if __name__ == "__main__":
    main()
