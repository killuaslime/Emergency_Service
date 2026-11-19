"""Пользователи карты. Пароли хранятся только в виде хэша в map_users.json.

  python map_panel/manage_users.py add admin admin
  python map_panel/manage_users.py add kushina supervisor 51960972   # max_user_id супервайзера
  python map_panel/manage_users.py list
  python map_panel/manage_users.py remove kushina

Роль admin видит все магазины, supervisor только магазины со своим supervisor_id.
Секрет для .env: python -c "import secrets; print(secrets.token_urlsafe(48))"
"""
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

USERS_FILE = Path(__file__).resolve().parent / "map_users.json"
ITERATIONS = 300_000


def load() -> dict:
    return json.loads(USERS_FILE.read_text(encoding="utf-8")) if USERS_FILE.exists() else {}


def save(users: dict) -> None:
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(USERS_FILE, 0o600)


def make_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${digest.hex()}"


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else ""
    users = load()

    if cmd == "add" and len(argv) >= 4 and argv[3] in ("admin", "supervisor"):
        name, role = argv[2], argv[3]
        entry = {"role": role}
        if role == "supervisor":
            if len(argv) < 5:
                sys.exit("Для supervisor укажите max_user_id: add NAME supervisor 51960972")
            entry["supervisor_id"] = int(argv[4])
        password = getpass.getpass("Пароль (не короче 12 символов): ")
        if len(password) < 12:
            sys.exit("Пароль слишком короткий.")
        if password != getpass.getpass("Ещё раз: "):
            sys.exit("Пароли не совпали.")
        entry["hash"] = make_hash(password)
        users[name] = entry
        save(users)
        print(f"Пользователь {name} сохранён.")
    elif cmd == "list":
        for name, u in users.items():
            print(name, u["role"], u.get("supervisor_id", ""))
    elif cmd == "remove" and len(argv) >= 3:
        users.pop(argv[2], None)
        save(users)
        print("Удалён (его сессия перестанет работать сразу).")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
