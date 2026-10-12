"""
bot.py — Telegram-бот AdClipper на Telethon (MTProto, файлы до 2 ГБ).

Конвейер: [YouTube -> SponsorBlock] / [файл] -> выбор нарезки -> нарезка ->
раскладка (9:16 / ASMR снизу) -> баннер в середину -> поштучная отправка.
"""
import asyncio
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

import config
import fast_telethon as ft
import video_processor as vp
from config import (BANNERS_DIR, ASMR_DIR, MAX_FILE_SIZE, STATUS_UPDATE_INTERVAL, TEMP_DIR,
                    find_asmr, find_banner, get_user_settings, save_user_settings)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("bot")

client: Optional[TelegramClient] = None

# --- состояние ---------------------------------------------------------------
user_queues: Dict[int, asyncio.Queue] = {}
user_workers: Dict[int, asyncio.Task] = {}
current_jobs: Dict[int, asyncio.Task] = {}
pending_split_choices: Dict[int, asyncio.Future] = {}
awaiting_input: Dict[int, str] = {}  # "banner" | "asmr" | "youtube"

SPLIT_OPTIONS = [2, 3, 4, 5, 6, 8, 10, 12, 15]
AUTO_SPLIT_OPTIONS = [2, 3, 4, 5, 6, 8, 10, 12]
AD_DURATIONS = [1.5, 2.0, 3.0, 4.0, 5.0, 6.0]
SPLIT_CHOICE_TIMEOUT = 600
VIDEO_FILE_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp", ".ts"}


# =============================================================================
# Утилиты UI
# =============================================================================
def onoff(v: bool) -> str:
    return "✅ ВКЛ" if v else "❌ ВЫКЛ"


def bar(p: float, width: int = 12) -> str:
    p = max(0.0, min(1.0, p))
    full = int(round(p * width))
    return "▰" * full + "▱" * (width - full) + f" {p * 100:.0f}%"


def mb(x: float) -> str:
    return f"{x / 1024 / 1024:.1f} МБ"


class Status:
    """Статусное сообщение с автоматическим обновлением раз в N секунд."""

    def __init__(self, msg):
        self.msg = msg
        self.text = msg.message or ""
        self.buttons = None
        self._shown = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._hold_until = 0.0

    def start(self):
        self._task = asyncio.create_task(self._ticker())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush()

    async def _ticker(self):
        while True:
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)
            await self.flush()

    def set(self, text: str, buttons=None):
        self.text, self.buttons = text, buttons

    async def push(self, text: str, buttons=None):
        self.set(text, buttons)
        await self.flush()

    async def flush(self):
        key = (self.text, repr(self.buttons))
        if key == self._shown or time.monotonic() < self._hold_until:
            return
        async with self._lock:
            try:
                await self.msg.edit(self.text[:4096], buttons=self.buttons, link_preview=False)
                self._shown = key
            except MessageNotModifiedError:
                self._shown = key
            except FloodWaitError as e:
                self._hold_until = time.monotonic() + e.seconds + 1
            except Exception as e:
                log.debug("status edit failed: %s", e)


def main_menu(uid: int):
    s = get_user_settings(uid)
    banner = find_banner(uid)
    split = "Спрашивать" if s["split_mode"] == "ask" else f"Авто ({s['split_mode']} ч.)"
    fmt = [] if s["aspect"] == "original" else [s["aspect"]]
    if s["asmr_bottom"]:
        fmt.append("ASMR")
    rows = [
        [Button.inline("📺 Скачать с YouTube (без рекламы)", b"yt")],
        [Button.inline(f"📐 Формат: {' + '.join(fmt) if fmt else 'оригинал'}", b"fmt")],
        [Button.inline("🧹 Настройки YouTube / Анти-Спонсор", b"sb")],
        [Button.inline("📥 Загрузить/Сменить баннер", b"banner_up"),
         Button.inline("🖼 Мой баннер", b"banner_show")],
        [Button.inline(f"⏱ Длительность паузы: {s['ad_duration']:.1f} с", b"dur")],
        [Button.inline(f"✂️ Нарезка: {split}", b"split_menu")],
    ]
    if banner:
        rows.append([Button.inline("🗑 Удалить баннер", b"banner_del")])
    return rows


def main_text(uid: int) -> str:
    s = get_user_settings(uid)
    banner = find_banner(uid)
    asmr = find_asmr(uid)
    return (
        "🎬 **AdClipper**\n\n"
        "Пришли видеофайл (до 2 ГБ) или ссылку на YouTube — можно сразу пачкой.\n"
        "Бот вырежет спонсорские вставки, порежет на части, при желании сделает 9:16 "
        "или split-screen с ASMR-видео снизу и вставит твой баннер в середину каждой части.\n\n"
        f"🖼 Баннер: {'✅ установлен' if banner else '❌ нет'}\n"
        f"🎧 ASMR-видео: {'✅ загружено' if asmr else '❌ нет'}"
        f"{' (режим ВКЛ)' if s['asmr_bottom'] else ''}\n"
        f"🧹 Анти-спонсор: {onoff(s['sponsorblock_enabled'])}\n\n"
        "ℹ️ Используй только видео, на которые у тебя есть права."
    )


def fmt_menu(uid: int):
    s = get_user_settings(uid)
    rows = [
        [Button.inline(("• " if s["aspect"] == a else "") + lab, f"asp:{a}".encode())
         for a, lab in (("3:4", "3:4 (TikTok)"), ("9:16", "9:16"), ("original", "Оригинал"))],
        [Button.inline(f"🎧 ASMR-видео снизу: {onoff(s['asmr_bottom'])}", b"t_asmr")],
        [Button.inline("📥 Загрузить/Сменить ASMR-видео", b"asmr_up")],
    ]
    if find_asmr(uid):
        rows.append([Button.inline("🗑 Удалить ASMR-видео", b"asmr_del")])
    rows.append([Button.inline("⬅️ Назад", b"menu")])
    return rows


FMT_TEXT = ("📐 **Формат итогового видео**\n\n"
            "• **3:4** — 1080×1440 (720×960 для слабых исходников), видео вписывается "
            "с размытым фоном.\n"
            "• **9:16** — 1080×1920 / 720×1280 с размытым фоном.\n"
            "• **Оригинал** — пропорции не меняются.\n"
            "• **ASMR снизу** — split-screen в выбранном формате (для «Оригинал» — 3:4): "
            "твоё видео сверху, ASMR-видео снизу целиком, без обрезки (зациклено, без звука).")


def sb_menu(uid: int):
    s = get_user_settings(uid)
    rows = [
        [Button.inline(f"🧹 Авто-удаление спонсоров: {onoff(s['sponsorblock_enabled'])}", b"sb_t")],
        [Button.inline("Баннер в YouTube-видео: " +
                       ("✅ Вставлять" if s["insert_banner_on_youtube"] else "❌ Только чистить"),
                       b"sb_ins")],
    ]
    for cat, name in vp.SB_CATEGORIES.items():
        mark = "☑️" if s["sb_categories"].get(cat) else "⬜️"
        rows.append([Button.inline(f"{mark} {name}", f"sbc:{cat}".encode())])
    rows.append([Button.inline("⬅️ Назад", b"menu")])
    return rows


def dur_menu(uid: int):
    s = get_user_settings(uid)
    btns = [Button.inline(("• " if abs(s["ad_duration"] - d) < 0.01 else "") + f"{d:.1f} с",
                          f"dur:{d}".encode()) for d in AD_DURATIONS]
    return [btns[:3], btns[3:], [Button.inline("⬅️ Назад", b"menu")]]


def split_menu(uid: int):
    s = get_user_settings(uid)
    cur = s["split_mode"]

    def lab(v, text):
        return ("• " if str(cur) == str(v) else "") + text

    rows = [[Button.inline(lab("ask", "❓ Спрашивать для каждого видео"), b"sm:ask")],
            [Button.inline(lab(1, "Не делить (1)"), b"sm:1")]]
    btns = [Button.inline(lab(n, f"Авто: {n} ч."), f"sm:{n}".encode()) for n in AUTO_SPLIT_OPTIONS]
    rows += [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([Button.inline("⬅️ Назад", b"menu")])
    return rows


async def safe_edit(event, text, buttons):
    try:
        await event.edit(text, buttons=buttons, link_preview=False)
    except MessageNotModifiedError:
        pass


# =============================================================================
# Команды
# =============================================================================
async def cmd_start(event):
    if not event.is_private:
        return
    awaiting_input.pop(event.sender_id, None)
    await event.respond(main_text(event.sender_id), buttons=main_menu(event.sender_id),
                        link_preview=False)


async def cmd_time(event):
    if not event.is_private:
        return
    uid = event.sender_id
    arg = (event.pattern_match.group(1) or "").strip().replace(",", ".")
    try:
        val = float(arg)
        if not 0.5 <= val <= 30:
            raise ValueError
    except ValueError:
        await event.respond("Использование: `/time 3.5` (от 0.5 до 30 сек)")
        return
    s = get_user_settings(uid)
    s["ad_duration"] = round(val, 2)
    save_user_settings(uid, s)
    await event.respond(f"⏱ Длительность паузы под статичный баннер: **{val:.2f} с**")


async def cmd_cancel(event):
    if not event.is_private:
        return
    uid = event.sender_id
    q = user_queues.get(uid)
    dropped = 0
    if q:
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
    fut = pending_split_choices.pop(uid, None)
    if fut and not fut.done():
        fut.cancel()
    job = current_jobs.get(uid)
    running = bool(job and not job.done())
    if running:
        job.cancel()
    awaiting_input.pop(uid, None)
    await event.respond(f"🛑 Отменено. Текущая задача: {'остановлена' if running else 'нет'}, "
                        f"удалено из очереди: {dropped}.")


async def cmd_help(event):
    if not event.is_private:
        return
    await event.respond(
        "**Команды**\n"
        "/start — меню и настройки\n"
        "/time <сек> — длительность паузы под статичный баннер\n"
        "/cancel — остановить текущую задачу и очистить очередь\n\n"
        "**Баннер**: PNG/JPG (пауза = настройка) или WEBM с прозрачностью/MP4/GIF "
        "(пауза = длительность баннера, звук баннера сохраняется). "
        "Баннер должен занимать **не менее 25% экрана** — иначе он будет отклонён.")


# =============================================================================
# Inline-кнопки
# =============================================================================
async def on_callback(event):
    uid = event.sender_id
    data = event.data.decode("utf-8", errors="ignore")
    s = get_user_settings(uid)

    # выбор нарезки для текущего видео
    if data.startswith("split:"):
        fut = pending_split_choices.get(uid)
        if fut and not fut.done():
            fut.set_result(int(data.split(":")[1]))
            await event.answer("Принято ✅")
        else:
            await event.answer("Этот выбор уже не актуален", alert=False)
        return

    if data == "menu":
        awaiting_input.pop(uid, None)
        await safe_edit(event, main_text(uid), main_menu(uid))
    elif data == "yt":
        awaiting_input[uid] = "youtube"
        await event.answer()
        await event.respond("📺 Пришли одну или несколько ссылок на YouTube "
                            "(watch, youtu.be, shorts, live). Ссылки принимаются и без этой кнопки.")
        return
    elif data == "fmt":
        await safe_edit(event, FMT_TEXT, fmt_menu(uid))
    elif data.startswith("asp:"):
        a = data[4:]
        if a in ("original", "3:4", "9:16"):
            s["aspect"] = a
            save_user_settings(uid, s)
        await safe_edit(event, FMT_TEXT, fmt_menu(uid))
    elif data == "t_asmr":
        if not s["asmr_bottom"] and not find_asmr(uid):
            await event.answer("Сначала загрузи ASMR-видео", alert=True)
            return
        s["asmr_bottom"] = not s["asmr_bottom"]
        save_user_settings(uid, s)
        await safe_edit(event, FMT_TEXT, fmt_menu(uid))
    elif data == "asmr_up":
        awaiting_input[uid] = "asmr"
        await event.answer()
        await event.respond("🎧 Пришли ASMR-видео (минимум 3 сек). Оно будет зациклено "
                            "в нижней части кадра без звука. /cancel — отмена.")
        return
    elif data == "asmr_del":
        p = find_asmr(uid)
        if p:
            p.unlink(missing_ok=True)
        s["asmr_bottom"] = False
        save_user_settings(uid, s)
        await event.answer("ASMR-видео удалено")
        await safe_edit(event, FMT_TEXT, fmt_menu(uid))
    elif data == "sb":
        await safe_edit(event, "🧹 **YouTube / Анти-Спонсор**\n\nСегменты берутся из базы "
                        "SponsorBlock и глав YouTube с рекламными названиями.", sb_menu(uid))
    elif data == "sb_t":
        s["sponsorblock_enabled"] = not s["sponsorblock_enabled"]
        save_user_settings(uid, s)
        await safe_edit(event, "🧹 **YouTube / Анти-Спонсор**", sb_menu(uid))
    elif data == "sb_ins":
        s["insert_banner_on_youtube"] = not s["insert_banner_on_youtube"]
        save_user_settings(uid, s)
        await safe_edit(event, "🧹 **YouTube / Анти-Спонсор**", sb_menu(uid))
    elif data.startswith("sbc:"):
        cat = data[4:]
        if cat in vp.SB_CATEGORIES:
            s["sb_categories"][cat] = not s["sb_categories"].get(cat, False)
            save_user_settings(uid, s)
        await safe_edit(event, "🧹 **YouTube / Анти-Спонсор**", sb_menu(uid))
    elif data == "banner_up":
        awaiting_input[uid] = "banner"
        await event.answer()
        await event.respond(
            "📥 Пришли баннер:\n• PNG/JPG — картинка (лучше как **файл**, чтобы сохранить прозрачность)\n"
            "• WEBM с альфа-каналом / MP4 / GIF — анимированный\n\n"
            "⚠️ Баннер должен занимать **не менее 25% экрана**. /cancel — отмена.")
        return
    elif data == "banner_show":
        b = find_banner(uid)
        await event.answer()
        if b:
            await client.send_file(uid, str(b), caption="🖼 Твой текущий баннер", force_document=True)
        else:
            await event.respond("Баннер не установлен.")
        return
    elif data == "banner_del":
        b = find_banner(uid)
        if b:
            b.unlink(missing_ok=True)
        await event.answer("Баннер удалён")
        await safe_edit(event, main_text(uid), main_menu(uid))
    elif data == "dur":
        await safe_edit(event, "⏱ **Длительность паузы** под статичный баннер "
                        "(или `/time <сек>`):", dur_menu(uid))
    elif data.startswith("dur:"):
        s["ad_duration"] = float(data[4:])
        save_user_settings(uid, s)
        await safe_edit(event, "⏱ **Длительность паузы**", dur_menu(uid))
    elif data == "split_menu":
        await safe_edit(event, "✂️ **Режим нарезки**", split_menu(uid))
    elif data.startswith("sm:"):
        v = data[3:]
        s["split_mode"] = "ask" if v == "ask" else int(v)
        save_user_settings(uid, s)
        await safe_edit(event, "✂️ **Режим нарезки**", split_menu(uid))
    try:
        await event.answer()
    except Exception:
        pass


# =============================================================================
# Загрузка баннера / ASMR
# =============================================================================
MIME_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "image/gif": ".gif", "image/png": ".png",
    "image/jpeg": ".jpg", "image/webp": ".webp", "video/x-msvideo": ".avi",
}


def _media_ext(event) -> str:
    """Расширение медиа: имя файла -> mime -> тип сообщения."""
    if event.photo:
        return ".jpg"
    f = event.file
    ext = ""
    if f:
        if f.name and "." in f.name:
            ext = "." + f.name.rsplit(".", 1)[1].lower()
        if not ext or len(ext) > 6:
            ext = MIME_EXT.get((f.mime_type or "").lower(), "") or (f.ext or "").lower()
    if not ext and (event.video or event.gif):
        ext = ".mp4"
    if event.sticker and ext not in (".webm", ".webp"):
        ext = ""
    return {".jpe": ".jpg", ".jpeg": ".jpg", ".m4v": ".mp4"}.get(ext, ext)


async def handle_banner_upload(event):
    uid = event.sender_id
    ext = _media_ext(event)
    if not ext or vp.banner_kind(ext) is None:
        await event.respond("❌ Формат не поддерживается. Нужен PNG/JPG/WEBP или WEBM/MP4/GIF.")
        return
    if event.file and event.file.size and event.file.size > 200 * 1024 * 1024:
        await event.respond("❌ Баннер слишком большой (макс. 200 МБ).")
        return

    tmp = TEMP_DIR / f"banner_{uid}_{uuid.uuid4().hex[:6]}{ext}"
    msg = await event.respond("⏳ Проверяю баннер…")
    try:
        await event.download_media(file=str(tmp))
        meta = await vp.analyze_banner(tmp, TEMP_DIR)
        ok, cov, text = vp.check_banner_coverage(meta)
        if not ok:
            await msg.edit(f"❌ {text}.\nСделай баннер крупнее или с меньшими прозрачными "
                           "полями и пришли снова.")
            return
        # реальное покрытие на типовых кадрах
        cov_t = vp.banner_screen_coverage(meta, 1080, 1440)
        cov_v = vp.banner_screen_coverage(meta, 720, 1280)
        cov_h = vp.banner_screen_coverage(meta, 1920, 1080)
        for old in BANNERS_DIR.glob(f"{uid}.*"):
            old.unlink(missing_ok=True)
        dst = BANNERS_DIR / f"{uid}{ext}"
        shutil.move(str(tmp), dst)
        awaiting_input.pop(uid, None)

        warn = ""
        if min(meta["width"], meta["height"]) < 360:
            warn = "\n⚠️ Низкое разрешение — на видео баннер будет размыт."
        bad = [n for n, c in (("3:4", cov_t), ("9:16", cov_v), ("16:9", cov_h))
               if c < config.MIN_BANNER_SCREEN_RATIO]
        if bad:
            warn += (f"\n⚠️ Из-за пропорций на кадре {', '.join(bad)} баннер займёт меньше 25% — "
                     "для таких видео он будет пропущен.")
        kind = (f"видео, {meta['duration']:.1f} с, звук: {'да' if meta['has_audio'] else 'нет'}"
                if meta["kind"] == "video" else "картинка")
        await msg.edit(
            f"✅ Баннер сохранён ({kind}, {meta['width']}×{meta['height']}).\n"
            f"Непрозрачная часть: {meta['opaque_ratio'] * 100:.0f}%\n"
            f"Покрытие кадра: 3:4 — {cov_t * 100:.0f}%, 9:16 — {cov_v * 100:.0f}%, "
            f"16:9 — {cov_h * 100:.0f}%{warn}",
            buttons=[[Button.inline("⬅️ В меню", b"menu")]])
    except Exception as e:
        log.exception("banner upload failed")
        await msg.edit(f"❌ Не удалось обработать баннер: {e}")
    finally:
        tmp.unlink(missing_ok=True)


async def handle_asmr_upload(event):
    uid = event.sender_id
    ext = _media_ext(event)
    if ext not in VIDEO_FILE_EXTS | {".gif"}:
        await event.respond("❌ Нужен видеофайл (MP4/MOV/MKV/WEBM).")
        return
    if event.file and event.file.size and event.file.size > 500 * 1024 * 1024:
        await event.respond("❌ ASMR-видео слишком большое (макс. 500 МБ).")
        return
    tmp = TEMP_DIR / f"asmr_{uid}_{uuid.uuid4().hex[:6]}{ext}"
    msg = await event.respond("⏳ Сохраняю ASMR-видео…")
    try:
        await ft.fast_download(client, event.document, str(tmp))
        info = await vp.get_media_info(tmp)
        if not info["is_video"] or info["duration"] < 3:
            await msg.edit("❌ Видео не распознано или короче 3 секунд.")
            return
        for old in ASMR_DIR.glob(f"{uid}.*"):
            old.unlink(missing_ok=True)
        shutil.move(str(tmp), ASMR_DIR / f"{uid}{ext}")
        s = get_user_settings(uid)
        s["asmr_bottom"] = True
        save_user_settings(uid, s)
        awaiting_input.pop(uid, None)
        await msg.edit(f"✅ ASMR-видео сохранено ({info['width']}×{info['height']}, "
                       f"{vp.fmt_time(info['duration'])}). Режим ASMR снизу включён.",
                       buttons=[[Button.inline("⬅️ В меню", b"menu")]])
    except Exception as e:
        log.exception("asmr upload failed")
        await msg.edit(f"❌ Ошибка: {e}")
    finally:
        tmp.unlink(missing_ok=True)


# =============================================================================
# Очередь
# =============================================================================
async def enqueue(uid: int, task: dict, event):
    q = user_queues.setdefault(uid, asyncio.Queue())
    await q.put(task)
    busy = uid in current_jobs and not current_jobs[uid].done()
    pos = q.qsize() + (1 if busy else 0)
    label = task.get("title") or task.get("url") or "видео"
    await event.reply(f"📥 В очереди: **#{pos}** — {label}", link_preview=False)
    if uid not in user_workers:
        user_workers[uid] = asyncio.create_task(worker(uid))


async def worker(uid: int):
    q = user_queues[uid]
    while True:
        try:
            task = await asyncio.wait_for(q.get(), timeout=30)
        except asyncio.TimeoutError:
            if q.empty():  # без await между проверкой и удалением — гонки нет
                user_workers.pop(uid, None)
                return
            continue
        job = asyncio.create_task(split_insert_banner_and_send_separately(uid, task))
        current_jobs[uid] = job
        try:
            await job
        except asyncio.CancelledError:
            if not job.cancelled():
                raise
        except Exception:
            log.exception("job crashed")
        finally:
            current_jobs.pop(uid, None)
            q.task_done()


# =============================================================================
# Выбор нарезки
# =============================================================================
async def ask_split_choice(uid: int, st: Status, duration: float, title: str,
                           ad_len: Optional[float]) -> int:
    lines = [f"✂️ **{title}**", f"Длительность: **{vp.fmt_time(duration)}**", "",
             "На сколько частей разделить?"]
    for n in SPLIT_OPTIONS:
        part = duration / n
        if part < 3:
            continue
        ad = f" · баннер на {vp.fmt_time(part / 2)}" if ad_len else ""
        lines.append(f"• {n} ч. по ~{vp.fmt_time(part)}{ad}")
    lines.append("\nИли отправь число от 1 до 50 сообщением.")
    btns = [Button.inline(f"{n}", f"split:{n}".encode()) for n in SPLIT_OPTIONS
            if duration / n >= 3]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    rows.append([Button.inline("▶️ Не делить (1 видео целиком)", b"split:1")])

    fut = asyncio.get_running_loop().create_future()
    pending_split_choices[uid] = fut
    await st.push("\n".join(lines), buttons=rows)
    try:
        n = await asyncio.wait_for(fut, timeout=SPLIT_CHOICE_TIMEOUT)
    except asyncio.TimeoutError:
        n = 1
    finally:
        pending_split_choices.pop(uid, None)
    return max(1, min(int(n), 50))


# =============================================================================
# Главный конвейер
# =============================================================================
async def split_insert_banner_and_send_separately(uid: int, task: dict):
    s = get_user_settings(uid)
    chat_id = task["chat_id"]
    work = TEMP_DIR / f"{uid}_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    status_msg = await client.send_message(chat_id, "⏳ Начинаю обработку…",
                                           reply_to=task.get("reply_to"), link_preview=False)
    st = Status(status_msg)
    st.start()

    is_yt = task["type"] == "youtube"
    title = task.get("title") or "Видео"
    removed_sec, removed_list = 0.0, []
    sent = 0

    try:
        # ---------- 0. получение исходника ----------
        if is_yt:
            prog = {"d": 0, "t": 0}

            def yt_hook(d, t):
                prog["d"], prog["t"] = d, t

            dl = asyncio.create_task(vp.download_youtube_video(task["url"], work / "source.mp4", yt_hook))
            while not dl.done():
                p = prog["d"] / prog["t"] if prog["t"] else 0
                size = f"{mb(prog['d'])} / {mb(prog['t'])}" if prog["t"] else mb(prog["d"])
                st.set(f"📺 Скачиваю с YouTube…\n{bar(p)}\n{size}")
                await asyncio.wait({dl}, timeout=1)
            meta = dl.result()
            title = meta["title"]
            source = Path(meta["file_path"])

            if s["sponsorblock_enabled"]:
                await st.push(f"🧹 **{title}**\nИщу спонсорские вставки (SponsorBlock + главы)…")
                cats = [c for c, on in s["sb_categories"].items() if on]
                info0 = await vp.get_media_info(source)
                segs = await vp.fetch_sponsorblock_segments(
                    meta["video_id"], cats, info0["duration"] or meta["duration"], meta["chapters"])
                if segs:
                    removed_list = [f"{vp.fmt_time(x['start'])}–{vp.fmt_time(x['end'])} ({x['category']})"
                                    for x in segs]
                    head = (f"🧹 **{title}**\nНайдено вставок: **{len(segs)}**\n" +
                            "\n".join("• " + r for r in removed_list[:15]))

                    async def sb_cb(p):
                        st.set(head + f"\n\nВырезаю…\n{bar(p)}")

                    clean = work / "clean.mp4"
                    _, removed_sec = await vp.remove_sponsor_segments(source, clean, segs, sb_cb)
                    source.unlink(missing_ok=True)
                    source = clean
                    await st.push(head + f"\n\n✅ Вырезано {vp.fmt_time(removed_sec)}")
                else:
                    await st.push(f"🧹 **{title}**\nСпонсорских вставок не найдено.")
        else:
            msg = task["message"]
            size = msg.file.size or 0
            if size > MAX_FILE_SIZE:
                raise RuntimeError("Файл больше 2 ГБ")
            source = work / f"source{msg.file.ext or '.mp4'}"
            prog = {"d": 0, "t": size}

            def tg_hook(d, t):
                prog["d"], prog["t"] = d, t or size

            dl = asyncio.create_task(ft.fast_download(client, msg.document, str(source), tg_hook))
            while not dl.done():
                p = prog["d"] / prog["t"] if prog["t"] else 0
                st.set(f"📥 Скачиваю файл…\n{bar(p)}\n{mb(prog['d'])} / {mb(prog['t'])}")
                await asyncio.wait({dl}, timeout=1)
            dl.result()

        info = await vp.get_media_info(source)
        if not info["is_video"]:
            raise RuntimeError("Файл не распознан как видео")

        # ---------- баннер и раскладка ----------
        banner = find_banner(uid)
        use_banner = bool(banner) and (not is_yt or s["insert_banner_on_youtube"])
        banner_meta, notes = None, []
        if use_banner:
            try:
                banner_meta = await vp.analyze_banner(banner, work)
                ok, _, msg_cov = vp.check_banner_coverage(banner_meta)
                if not ok:
                    notes.append(f"⚠️ {msg_cov} — баннер пропущен")
                    use_banner = False
            except Exception as e:
                notes.append(f"⚠️ Баннер не читается ({e}) — пропущен")
                use_banner = False
        asmr = find_asmr(uid) if s["asmr_bottom"] else None
        if s["asmr_bottom"] and not asmr:
            notes.append("⚠️ ASMR-режим включён, но видео не загружено")

        ad_len = None
        if use_banner:
            ad_len = banner_meta["duration"] if (banner_meta["kind"] == "video"
                                                 and banner_meta["duration"] > 0.1) else s["ad_duration"]

        # ---------- выбор нарезки ----------
        if s["split_mode"] == "ask":
            n = await ask_split_choice(uid, st, info["duration"], title, ad_len)
        else:
            n = int(s["split_mode"])
        n = max(1, min(n, max(1, int(info["duration"] // 2))))

        # ---------- этап 1: нарезка ----------
        async def split_cb(p):
            st.set(f"✂️ **{title}**\nЭтап 1/4 — нарезка на {n} ч.\n{bar(p)}")

        await st.push(f"✂️ **{title}**\nЭтап 1/4 — нарезка на {n} ч.\n{bar(0)}")
        parts = await vp.split_video_into_parts(source, work / "parts", n, progress_cb=split_cb)
        source.unlink(missing_ok=True)

        # ---------- этапы 2–4 по каждой части ----------
        for part in parts:
            i = part["index"]
            head = f"🎬 **{title}**\nЧасть **{i}/{n}**"
            cur = Path(part["path"])

            # этап 2: раскладка
            layout, aspect = "original", "original"
            if asmr or s["aspect"] != "original":
                async def lay_cb(p, _h=head):
                    st.set(f"{_h}\nЭтап 2/4 — формат кадра\n{bar(p)}")

                res = await vp.apply_layout(cur, work / f"layout_{i:03d}.mp4",
                                            s["aspect"], asmr, lay_cb)
                layout, aspect = res["layout"], res["aspect"]
                if Path(res["path"]) != cur:
                    cur.unlink(missing_ok=True)
                    cur = Path(res["path"])

            # этап 3: баннер
            banner_res = None
            if use_banner:
                async def ad_cb(p, _h=head):
                    st.set(f"{_h}\nЭтап 3/4 — вставка баннера в середину\n{bar(p)}")

                out = work / f"banner_{i:03d}.mp4"
                banner_res = await vp.insert_banner_at_middle(cur, banner, out, s["ad_duration"],
                                                              banner_meta, ad_cb)
                cur.unlink(missing_ok=True)
                cur = out

            # этап 4: отправка
            final = work / f"{_safe_name(title)}_part{i}of{n}.mp4"
            os.replace(cur, final)
            caption = build_caption(title, is_yt, removed_sec, len(removed_list), part, n,
                                    layout, aspect, banner_res, await vp.get_media_info(final))
            await send_video(chat_id, final, caption, st, head, work)
            final.unlink(missing_ok=True)
            sent += 1

        tail = ("\n" + "\n".join(notes)) if notes else ""
        await st.push(f"✅ **{title}**\nГотово: отправлено {sent} из {n} ч.{tail}")
    except asyncio.CancelledError:
        st.set(f"🛑 **{title}** — отменено (отправлено частей: {sent}).")
        raise
    except Exception as e:
        log.exception("pipeline error")
        err = str(e)
        if len(err) > 700:
            err = err[:300] + " … " + err[-380:]
        st.set(f"❌ **{title}**\nОшибка: `{err}`\nОтправлено частей: {sent}")
    finally:
        await st.stop()
        shutil.rmtree(work, ignore_errors=True)


def _safe_name(title: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    return (keep[:40] or "video").replace(" ", "_")


def build_caption(title, is_yt, removed_sec, removed_cnt, part, n, layout, aspect,
                  banner_res, info) -> str:
    lines = [f"🎬 {title}"]
    if is_yt:
        lines.append(f"🧹 Вырезано рекламы: {vp.fmt_time(removed_sec)} ({removed_cnt} вст.)"
                     if removed_cnt else "🧹 Спонсорских вставок не найдено")
    lines.append(f"📦 Часть {part['index']} из {n} · "
                 f"{vp.fmt_time(part['start'])}–{vp.fmt_time(part['end'])}")
    lines.append(f"⏱ Итоговая длительность: {vp.fmt_time(info['duration'])}")
    lay = {"original": "оригинал", "fit": f"{aspect} (размытый фон)",
           "asmr": f"{aspect} split-screen (ASMR снизу)"}[layout]
    lines.append(f"📐 Формат: {lay} · {info['width']}×{info['height']}")
    if banner_res:
        if banner_res["inserted"]:
            lines.append(f"🖼 Баннер: на {vp.fmt_time(banner_res['mid_time'])}, "
                         f"{banner_res['ad_duration']:.1f} с, покрытие {banner_res['coverage'] * 100:.0f}%")
        else:
            lines.append(f"🖼 Баннер пропущен: {banner_res['reason']}")
    return "\n".join(lines)[:1024]


async def send_video(chat_id, path: Path, caption: str, st: Status, head: str, work: Path):
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise RuntimeError(f"Часть весит {mb(size)} — больше 2 ГБ. Выбери больше частей.")
    info = await vp.get_media_info(path)
    thumb = await vp.make_thumbnail(path, work / f"{path.stem}_thumb.jpg")

    def up_cb(cur, total):
        st.set(f"{head}\nЭтап 4/4 — отправка\n{bar(cur / total if total else 0)}\n"
               f"{mb(cur)} / {mb(total)}")

    input_file = await ft.fast_upload(client, str(path), up_cb)
    await client.send_file(
        chat_id, input_file, caption=caption, thumb=thumb, supports_streaming=True,
        attributes=[DocumentAttributeVideo(duration=int(round(info["duration"])),
                                           w=info["width"], h=info["height"],
                                           supports_streaming=True)])
    if thumb:
        Path(thumb).unlink(missing_ok=True)


# =============================================================================
# Входящие сообщения
# =============================================================================
def _is_video_message(event) -> bool:
    doc = event.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    ext = (event.file.ext or "").lower() if event.file else ""
    if event.sticker:
        return False
    return mime.startswith("video/") or ext in VIDEO_FILE_EXTS


async def on_message(event):
    if not event.is_private:
        return
    text = event.raw_text or ""
    if text.startswith("/"):
        return  # команды обрабатываются отдельными хендлерами
    uid = event.sender_id
    media = event.media
    is_webpage = media is not None and getattr(media, "webpage", None) is not None

    # число в ответ на вопрос о нарезке
    fut = pending_split_choices.get(uid)
    if fut and not fut.done() and (not media or is_webpage) and text.strip().isdigit():
        n = int(text.strip())
        if 1 <= n <= 50:
            fut.set_result(n)
            await event.reply(f"✂️ Делю на {n} ч.")
        else:
            await event.reply("Нужно число от 1 до 50.")
        return

    mode = awaiting_input.get(uid)
    if mode == "banner" and media and not is_webpage:
        await handle_banner_upload(event)
        return
    if mode == "asmr" and media and not is_webpage:
        await handle_asmr_upload(event)
        return

    # ссылки YouTube (Telegram добавляет превью -> media.webpage)
    if not media or is_webpage:
        links = vp.extract_youtube_links(text)
        if links:
            awaiting_input.pop(uid, None)
            for url, vid in links:
                await enqueue(uid, {"type": "youtube", "url": url, "video_id": vid,
                                    "chat_id": event.chat_id, "reply_to": event.id,
                                    "title": f"YouTube {vid}"}, event)
            return
        if mode == "youtube":
            await event.reply("Не вижу ссылки на YouTube. Пример: https://youtu.be/dQw4w9WgXcQ")
        elif text.strip():
            await event.reply("Пришли видеофайл или ссылку на YouTube. Меню — /start")
        return

    if _is_video_message(event):
        if (event.file.size or 0) > MAX_FILE_SIZE:
            await event.reply("❌ Файл больше 2 ГБ — это лимит Telegram.")
            return
        name = event.file.name or f"Видео {event.id}"
        await enqueue(uid, {"type": "file", "message": event.message, "chat_id": event.chat_id,
                            "reply_to": event.id, "title": name}, event)
        return

    await event.reply("Это не видео. Пришли видеофайл или ссылку на YouTube.")


# =============================================================================
# Запуск
# =============================================================================
async def main():
    global client
    token = config.BOT_TOKEN
    if not token or "ВСТАВЬ" in token or ":" not in token:
        raise SystemExit("❌ Не задан BOT_TOKEN (переменная окружения или config.py)")

    for p in TEMP_DIR.iterdir():  # чистим хвосты прошлых запусков
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    log.info("FFmpeg: %s", config.FFMPEG_EXE)
    log.info("cryptg (быстрый AES): %s", "да" if ft.CRYPTG_OK else "НЕТ — pip install cryptg")

    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH,
                            connection_retries=None, retry_delay=3)
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
    client.add_event_handler(cmd_time, events.NewMessage(pattern=r"^/time(?:@\w+)?(?:\s+(.+))?$"))
    client.add_event_handler(cmd_cancel, events.NewMessage(pattern=r"^/cancel(?:@\w+)?$"))
    client.add_event_handler(on_callback, events.CallbackQuery())
    client.add_event_handler(on_message, events.NewMessage(incoming=True))

    await client.start(bot_token=token)
    me = await client.get_me()
    log.info("Бот запущен: @%s", me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass