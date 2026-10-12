"""
Конфигурация проекта AdClipper.
Пути к хранилищам, токен бота, публичные MTProto-ключи Telegram.
"""
import os
import json
from pathlib import Path

# --- Базовые пути ------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# DATA_DIR — постоянный том хостинга (на Bothost это /app/data), иначе ./storage
STORAGE_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "storage")).resolve()
BANNERS_DIR = STORAGE_DIR / "banners"
TEMP_DIR = STORAGE_DIR / "temp"
SETTINGS_FILE = STORAGE_DIR / "settings.json"
BIN_DIR = BASE_DIR / "bin"
SESSION_DIR = STORAGE_DIR / "session"
ASMR_DIR = STORAGE_DIR / "asmr"

for _d in (STORAGE_DIR, BANNERS_DIR, TEMP_DIR, BIN_DIR, SESSION_DIR, ASMR_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Telegram ----------------------------------------------------------------
# Публичные официальные MTProto-ключи Telegram Desktop.
# Пользователю нужно вписать только BOT_TOKEN.
API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")

# Токен бота берём из окружения, иначе из прямого присвоения ниже.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or "ВСТАВЬ_СЮДА_ТОКЕН_БОТА"

SESSION_NAME = str(SESSION_DIR / "adclipper_bot")

# --- Ограничения -------------------------------------------------------------
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 ГБ — лимит MTProto
MIN_BANNER_SCREEN_RATIO = 0.25          # баннер должен занимать >= 25% экрана
DEFAULT_AD_DURATION = 3.0               # пауза под статичный баннер, сек
STATUS_UPDATE_INTERVAL = 3.0            # как часто обновлять статус, сек

# --- FFmpeg ------------------------------------------------------------------
def _resolve_ffmpeg() -> str:
    """Ищем ffmpeg: локальный bin/ -> imageio-ffmpeg -> системный."""
    import shutil
    import platform

    exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    local = BIN_DIR / exe
    if local.exists():
        return str(local)

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff

    return "ffmpeg"  # последняя надежда — в PATH


FFMPEG_EXE = _resolve_ffmpeg()

# --- Настройки пользователей -------------------------------------------------
DEFAULT_USER_SETTINGS = {
    # SponsorBlock / YouTube
    "sponsorblock_enabled": True,
    "insert_banner_on_youtube": True,
    "sb_categories": {
        "sponsor": True,
        "selfpromo": True,
        "interaction": True,
        "intro": False,
        "outro": False,
    },
    # Формат
    "aspect": "3:4",               # "original" | "3:4" | "9:16" — итоговый формат кадра
    "vertical_9_16": False,        # устаревшее, оставлено для совместимости
    "asmr_bottom": False,          # ASMR-дорожка снизу кадра
    # Баннер
    "ad_duration": DEFAULT_AD_DURATION,
    # Нарезка: "ask" или целое число частей
    "split_mode": "ask",
}


def _load_all_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_all_settings(data: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_FILE)


def get_user_settings(user_id: int) -> dict:
    """Вернуть настройки пользователя, дополнив дефолтами."""
    data = _load_all_settings()
    raw = data.get(str(user_id), {})
    merged = json.loads(json.dumps(DEFAULT_USER_SETTINGS))  # deep copy
    for k, v in raw.items():
        if k == "sb_categories" and isinstance(v, dict):
            merged["sb_categories"].update(v)
        else:
            merged[k] = v
    if "aspect" not in raw:  # миграция старых настроек
        merged["aspect"] = "9:16" if raw.get("vertical_9_16") else DEFAULT_USER_SETTINGS["aspect"]
    if merged["aspect"] not in ("original", "3:4", "9:16"):
        merged["aspect"] = "3:4"
    return merged


def save_user_settings(user_id: int, settings: dict) -> None:
    data = _load_all_settings()
    data[str(user_id)] = settings
    _save_all_settings(data)


def find_banner(user_id: int):
    """Вернуть путь к баннеру пользователя или None."""
    for p in BANNERS_DIR.glob(f"{user_id}.*"):
        if p.is_file():
            return p
    return None

def find_asmr(user_id: int):
    """Вернуть путь к ASMR-видео пользователя (для нижней половины кадра) или None."""
    for p in ASMR_DIR.glob(f"{user_id}.*"):
        if p.is_file():
            return p
    return None
