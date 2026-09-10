"""Guild-scoped verification state, including lazy migration of existing codes."""
import datetime
import math
import secrets
import string
import time

from storage import BASE_DIR, load_json, save_json

CODE_FILE = BASE_DIR / "verify_codes.json"
MAX_VERIFY_ATTEMPTS = 3
VERIFY_COOLDOWN_SECONDS = 600
CODE_TTL_SECONDS = 30 * 60


def key(user_id, guild_id):
    return f"{guild_id}:{user_id}"


def get_pending_code(user_id, guild_id):
    data = load_json(CODE_FILE)
    record = data.get(key(user_id, guild_id))
    if record is None:
        legacy = data.get(str(user_id))
        if legacy and str(legacy.get("guild_id")) == str(guild_id):
            record = dict(legacy)
            # Old verified flags did not guarantee a successful role grant.
            # Recheck once; never remove an existing Discord role.
            record["legacy_verified"] = record.get("verified", False)
            record["verified"] = False
            data[key(user_id, guild_id)] = record
            del data[str(user_id)]
            save_json(CODE_FILE, data)
    return record


def generate_code():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))


def code_expired(record):
    try:
        created = datetime.datetime.fromisoformat(record["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        age = time.time() - created.timestamp()
        return age < 0 or age >= CODE_TTL_SECONDS
    except (KeyError, TypeError, ValueError):
        return True


def save_pending_code(user_id, guild_id, code):
    previous = get_pending_code(user_id, guild_id) or {}
    data = load_json(CODE_FILE)
    data[key(user_id, guild_id)] = {
        "code": code, "guild_id": guild_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "verified": False, "attempts": previous.get("attempts", 0),
        "cooldown_until": previous.get("cooldown_until", 0),
    }
    save_json(CODE_FILE, data)


def get_cooldown_remaining(user_id, guild_id):
    pending = get_pending_code(user_id, guild_id) or {}
    until = float(pending.get("cooldown_until", 0))
    if until and until <= time.time():
        data = load_json(CODE_FILE)
        data[key(user_id, guild_id)].update(attempts=0, cooldown_until=0)
        save_json(CODE_FILE, data)
    return max(0, math.ceil(until - time.time()))


def record_verification_failure(user_id, guild_id):
    get_cooldown_remaining(user_id, guild_id)
    data = load_json(CODE_FILE)
    pending = data[key(user_id, guild_id)]
    attempts = min(MAX_VERIFY_ATTEMPTS, int(pending.get("attempts", 0)) + 1)
    until = time.time() + VERIFY_COOLDOWN_SECONDS if attempts >= MAX_VERIFY_ATTEMPTS else 0
    pending.update(attempts=attempts, cooldown_until=until)
    save_json(CODE_FILE, data)
    return attempts, max(0, math.ceil(until - time.time()))


def mark_verified(user_id, guild_id, role_id, character_url):
    data = load_json(CODE_FILE)
    data[key(user_id, guild_id)].update(
        verified=True, role_id=role_id, character_url=character_url,
        verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        attempts=0, cooldown_until=0,
    )
    save_json(CODE_FILE, data)
