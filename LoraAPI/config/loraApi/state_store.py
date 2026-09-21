import copy
import json
from pathlib import Path


DEFAULT_STATE = {
    "users": [
        {"username": "PRIVY", "password": "@dmin123"},
        {"username": "Anesu", "password": "@nesu123"},
        {"username": "Admin", "password": "@dm1n4182"},
    ],
    "cancelled_invoice_count": 0,
}


def get_state_path() -> Path:
    return Path(__file__).resolve().parent / "runtime_state.json"


def load_state():
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if not state_path.exists():
        save_state(copy.deepcopy(DEFAULT_STATE))
        return copy.deepcopy(DEFAULT_STATE)

    try:
        with state_path.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (json.JSONDecodeError, TypeError):
        save_state(copy.deepcopy(DEFAULT_STATE))
        return copy.deepcopy(DEFAULT_STATE)

    normalized = copy.deepcopy(DEFAULT_STATE)
    normalized["users"] = state.get("users", DEFAULT_STATE["users"])
    normalized["cancelled_invoice_count"] = int(state.get("cancelled_invoice_count", 0))
    save_state(normalized)
    return copy.deepcopy(normalized)


def save_state(state):
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with state_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def sync_users_to_state(password_overrides=None):
    from django.contrib.auth import get_user_model

    password_overrides = password_overrides or {}
    User = get_user_model()
    state = load_state()
    existing_users = {
        str(entry.get('username', '')).strip(): str(entry.get('password', '')).strip()
        for entry in state.get('users', [])
        if str(entry.get('username', '')).strip()
    }

    final_users = []
    for user in User.objects.order_by('username'):
        username = str(user.username).strip()
        if not username:
            continue

        if username == 'Admin':
            password = '@dm1n4182'
        else:
            password = password_overrides.get(username, existing_users.get(username, username))

        final_users.append({
            'username': username,
            'password': str(password),
        })

    state['users'] = sorted(final_users, key=lambda entry: entry['username'].lower())
    save_state(state)
    return state['users']


def get_cancelled_invoice_count():
    return int(load_state().get("cancelled_invoice_count", 0))


def set_cancelled_invoice_count(value):
    state = load_state()
    state["cancelled_invoice_count"] = int(value)
    save_state(state)
    return state["cancelled_invoice_count"]
