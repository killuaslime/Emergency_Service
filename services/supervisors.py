
import json
from pathlib import Path


SUPERVISORS_FILE = (
    Path(__file__).resolve().parent.parent / "supervisors.json"
)


def load_supervisors() -> list[dict]:
    """Загружает список супервайзеров из JSON."""

    if not SUPERVISORS_FILE.exists():
        raise FileNotFoundError(
            f"Файл супервайзеров не найден: {SUPERVISORS_FILE}"
        )

    with open(SUPERVISORS_FILE, "r", encoding="utf-8") as file:
        supervisors = json.load(file)

    if not isinstance(supervisors, list):
        raise ValueError(
            "supervisors.json должен содержать список супервайзеров"
        )

    return supervisors


def normalize_name(name: str) -> str:
    """Нормализует ФИО для сравнения."""

    return " ".join(name.casefold().split())


def get_supervisor_by_name(name: str) -> dict | None:
    """Ищет супервайзера по ФИО."""

    normalized_name = normalize_name(name)

    for supervisor in load_supervisors():
        supervisor_name = normalize_name(
            supervisor.get("name", "")
        )

        if supervisor_name == normalized_name:
            return supervisor

    return None


def get_supervisor_id(name: str) -> int | None:
    """Возвращает MAX ID супервайзера по ФИО."""

    supervisor = get_supervisor_by_name(name)

    if supervisor is None:
        return None

    return supervisor.get("max_user_id")

def get_supervisor_by_id(max_user_id: int) -> dict | None:
    """Ищет супервайзера по MAX ID."""

    for supervisor in load_supervisors():
        if int(supervisor.get("max_user_id", 0)) == int(max_user_id):
            return supervisor

    return None