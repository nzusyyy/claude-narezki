"""
video_processor.py — ядро обработки видео AdClipper.

* метаданные без ffprobe (парсинг stderr `ffmpeg -i`)
* скачивание с YouTube (yt-dlp)
* SponsorBlock + главы YouTube -> вырезка спонсорских сегментов
* нарезка на N частей
* раскладка: 9:16 с размытым фоном и/или ASMR-видео снизу (split-screen)
* проверка баннера (>= 25% экрана) и вставка баннера в середину
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, Union

import aiohttp
from PIL import Image

from config import FFMPEG_EXE, MIN_BANNER_SCREEN_RATIO

log = logging.getLogger("video_processor")

ProgressCb = Optional[Callable[[float], Union[None, Awaitable[None]]]]  # 0.0..1.0

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_BANNER_EXTS = {".webm", ".mp4", ".mov", ".mkv", ".gif", ".m4v"}

SB_CATEGORIES = {
    "sponsor": "Спонсорская реклама",
    "selfpromo": "Самореклама блогера",
    "interaction": "Призывы подписаться/лайк",
    "intro": "Интро / Заставка",
    "outro": "Аутро / Титры",
}
AD_CHAPTER_KEYWORDS = ("реклама", "спонсор", "интеграция", "sponsor",
                       "paid promotion", "ad break")

# Целевые форматы кадра: (ширина, высота) для HD и для слабого исходника
ASPECTS = {
    "3:4": {"hd": (1080, 1440), "sd": (720, 960)},
    "9:16": {"hd": (1080, 1920), "sd": (720, 1280)},
}
ASMR_TOP_SHARE = 0.55  # доля высоты под основное видео в split-screen


def target_size(aspect: str, src_w: int, src_h: int) -> Tuple[int, int]:
    """1080p-ширина, если исходник это тянет (короткая сторона >= 720), иначе 720."""
    sizes = ASPECTS[aspect]
    return sizes["hd"] if min(src_w, src_h) >= 720 else sizes["sd"]


class FFmpegError(RuntimeError):
    pass


# =============================================================================
# Утилиты
# =============================================================================
def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    frac = sec - int(sec)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return base + (f".{int(frac * 10)}" if frac >= 0.05 else "")


def _even(x: float) -> int:
    v = int(round(x))
    return max(2, v - (v % 2))


async def _maybe_await(cb, *args):
    if cb is None:
        return
    try:
        r = cb(*args)
        if asyncio.iscoroutine(r):
            await r
    except Exception as e:
        log.debug("progress cb error: %s", e)


_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


async def run_ffmpeg(args: List[str], total_duration: Optional[float] = None,
                     progress_cb: ProgressCb = None) -> str:
    """Запустить ffmpeg асинхронно, отдавая прогресс 0..1. Возвращает хвост stderr."""
    cmd = [FFMPEG_EXE, "-hide_banner", "-nostdin", "-y", *args]
    log.debug("FFMPEG: %s", " ".join(map(str, cmd)))
    proc = await asyncio.create_subprocess_exec(
        *map(str, cmd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)

    tail: deque = deque(maxlen=60)
    buf = ""
    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for line in parts:
                if not line.strip():
                    continue
                tail.append(line)
                if total_duration and progress_cb:
                    m = _TIME_RE.search(line)
                    if m:
                        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        await _maybe_await(progress_cb, min(1.0, t / total_duration))
        if buf.strip():
            tail.append(buf)
        rc = await proc.wait()
    finally:
        if proc.returncode is None:  # отмена задачи -> не оставляем зомби-ffmpeg
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
    text = "\n".join(tail)
    if rc != 0:
        raise FFmpegError(f"ffmpeg завершился с кодом {rc}:\n{text[-1500:]}")
    await _maybe_await(progress_cb, 1.0)
    return text


# =============================================================================
# 2.1 Метаданные без ffprobe
# =============================================================================
async def get_media_info(file_path: Union[str, Path]) -> Dict:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_EXE, "-hide_banner", "-nostdin", "-i", str(file_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    text = err.decode("utf-8", errors="replace")
    return parse_media_info(text)


def parse_media_info(text: str) -> Dict:
    info = {
        "duration": 0.0, "width": 0, "height": 0, "fps": 30.0,
        "has_audio": False, "sample_rate": 44100, "channels": "stereo",
        "video_codec": None, "has_alpha": False, "is_video": False,
    }
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    rotation = 0
    for line in text.splitlines():
        if "Video:" in line and "attached pic" not in line and not info["width"]:
            vm = re.search(r"Video:\s*([A-Za-z0-9_]+)", line)
            if vm:
                info["video_codec"] = vm.group(1).lower()
            sm = re.search(r"[,\s](\d{2,5})x(\d{2,5})[,\s\]]", line + " ")
            if sm:
                info["width"], info["height"] = int(sm.group(1)), int(sm.group(2))
            fm = re.search(r"(\d+(?:\.\d+)?)\s*fps", line) or re.search(r"(\d+(?:\.\d+)?)\s*tbr", line)
            if fm:
                fps = float(fm.group(1))
                info["fps"] = fps if 10 <= fps <= 60 else (60.0 if fps > 60 else 30.0)
            if re.search(r"yuva|rgba|bgra|argb|abgr|pal8", line):
                info["has_alpha"] = True
        elif "Audio:" in line and not info["has_audio"]:
            info["has_audio"] = True
            am = re.search(r"(\d{4,6})\s*Hz", line)
            if am:
                info["sample_rate"] = int(am.group(1))
            info["channels"] = "mono" if re.search(r"Hz,\s*mono", line) else "stereo"
    if re.search(r"alpha_mode\s*:\s*1", text):
        info["has_alpha"] = True
    rm = re.search(r"rotate\s*:\s*(-?\d+)", text) or \
        re.search(r"rotation of\s*(-?\d+(?:\.\d+)?)", text)
    if rm:
        rotation = int(float(rm.group(1))) % 360
    if rotation in (90, 270):
        info["width"], info["height"] = info["height"], info["width"]

    info["is_video"] = info["duration"] > 0.1 and info["width"] > 0
    return info


def _alpha_decoder_args(info: Dict) -> List[str]:
    """Для WebM с альфой нужен libvpx-декодер, иначе прозрачность теряется."""
    if info.get("has_alpha"):
        if info.get("video_codec") == "vp9":
            return ["-c:v", "libvpx-vp9"]
        if info.get("video_codec") == "vp8":
            return ["-c:v", "libvpx"]
    return []


def _audio_layout(ch: str) -> str:
    return "mono" if ch == "mono" else "stereo"


def _venc(preset: str = "fast", crf: int = 20) -> List[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-video_track_timescale", "90000"]


def _aenc(sr: int, ch: str) -> List[str]:
    return ["-c:a", "aac", "-b:a", "192k", "-ar", str(sr),
            "-ac", "1" if ch == "mono" else "2"]


async def _concat_copy(files: List[Path], output: Path, work_dir: Path,
                       strip_metadata: bool = True) -> None:
    lst = work_dir / f"concat_{os.getpid()}_{id(files)}.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            sp = str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{sp}'\n")
    args = ["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy"]
    if strip_metadata:
        args += ["-map_metadata", "-1"]
    args += ["-movflags", "+faststart", str(output)]
    try:
        await run_ffmpeg(args)
    finally:
        lst.unlink(missing_ok=True)


async def encode_segment(src: Path, dst: Path, start: float, end: float,
                         w: int, h: int, fps: float, src_has_audio: bool,
                         want_audio: bool, sr: int, ch: str,
                         preset: str = "fast", crf: int = 20,
                         progress_cb: ProgressCb = None) -> None:
    """Перекодировать отрезок [start, end] в единый формат (для бесшовного concat)."""
    dur = max(0.05, end - start)
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    if want_audio and not src_has_audio:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}",
                 "-i", f"anullsrc=r={sr}:cl={_audio_layout(ch)}"]
    args += ["-map", "0:v:0"]
    if want_audio:
        args += ["-map", "0:a:0" if src_has_audio else "1:a:0"]
    args += ["-vf", f"scale={w}:{h},setsar=1", "-r", f"{fps:.3f}", *_venc(preset, crf)]
    if want_audio:
        args += [*_aenc(sr, ch), "-af", "aresample=async=1:first_pts=0"]
    else:
        args += ["-an"]
    args += ["-t", f"{dur:.3f}", str(dst)]
    await run_ffmpeg(args, dur, progress_cb)


# =============================================================================
# 2.2 YouTube
# =============================================================================
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})[^\s]*",
    re.IGNORECASE)


def extract_youtube_links(text: str) -> List[Tuple[str, str]]:
    """Вернуть [(url, video_id)] без дублей, в порядке появления."""
    seen, out = set(), []
    for m in YOUTUBE_RE.finditer(text or ""):
        vid = m.group(1)
        if vid not in seen:
            seen.add(vid)
            out.append((f"https://www.youtube.com/watch?v={vid}", vid))
    return out


def _download_youtube_sync(url: str, output_path: Path, progress_hook) -> Dict:
    import yt_dlp

    base = str(Path(output_path).with_suffix(""))

    def hook(d):
        if progress_hook and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            try:
                progress_hook(int(d.get("downloaded_bytes") or 0), int(total))
            except Exception:
                pass

    opts = {
        "format": ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                   "bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/"
                   "best[height<=1080]/best"),
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_EXE,
        "noplaylist": True,
        "outtmpl": base + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    file_path = None
    for rd in info.get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and os.path.exists(fp):
            file_path = fp
    if not file_path:
        cands = sorted(Path(base).parent.glob(Path(base).name + ".*"),
                       key=lambda p: p.stat().st_size, reverse=True)
        cands = [c for c in cands if c.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
        if not cands:
            raise RuntimeError("yt-dlp не создал выходной файл")
        file_path = str(cands[0])

    return {
        "video_id": info.get("id"),
        "title": info.get("title") or "YouTube video",
        "duration": float(info.get("duration") or 0),
        "chapters": info.get("chapters") or [],
        "file_path": file_path,
    }


async def download_youtube_video(url: str, output_path: Union[str, Path],
                                 progress_hook: Optional[Callable[[int, int], None]] = None) -> Dict:
    """yt-dlp синхронный — гоняем в отдельном потоке. progress_hook вызывается из потока."""
    return await asyncio.to_thread(_download_youtube_sync, url, Path(output_path), progress_hook)


# =============================================================================
# 2.3 SponsorBlock + главы
# =============================================================================
def _merge_segments(segs: List[Dict], gap: float = 0.4) -> List[Dict]:
    segs = sorted(segs, key=lambda s: s["start"])
    merged: List[Dict] = []
    for s in segs:
        if merged and s["start"] <= merged[-1]["end"] + gap:
            last = merged[-1]
            last["end"] = max(last["end"], s["end"])
            if s["category"] not in last["category"].split("+"):
                last["category"] += "+" + s["category"]
        else:
            merged.append(dict(s))
    return merged


async def fetch_sponsorblock_segments(video_id: str, categories: List[str],
                                      total_duration: float,
                                      chapters: Optional[List[Dict]] = None) -> List[Dict]:
    """Вернуть отсортированный, склеенный список {start, end, category} для удаления."""
    found: List[Dict] = []

    if categories:
        vid_hash = hashlib.sha256(video_id.encode("ascii")).hexdigest()[:4]
        params = urllib.parse.urlencode({
            "service": "YouTube",
            "categories": json.dumps(categories),
            "actionTypes": json.dumps(["skip", "full"]),
        })
        url = f"https://sponsor.ajay.app/api/skipSegments/{vid_hash}?{params}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        for item in data or []:
                            if item.get("videoID") != video_id:
                                continue
                            for seg in item.get("segments", []):
                                if seg.get("actionType") == "full":
                                    # «всё видео — реклама»: вырезать нечего, только пометка
                                    continue
                                a, b = seg.get("segment", [0, 0])
                                found.append({"start": float(a), "end": float(b),
                                              "category": seg.get("category", "sponsor")})
                    elif r.status != 404:  # 404 = сегментов нет
                        log.warning("SponsorBlock HTTP %s", r.status)
        except Exception as e:
            log.warning("SponsorBlock недоступен: %s", e)

    for ch in chapters or []:
        title = (ch.get("title") or "").lower()
        if any(k in title for k in AD_CHAPTER_KEYWORDS):
            found.append({"start": float(ch.get("start_time", 0)),
                          "end": float(ch.get("end_time", 0)), "category": "chapter"})

    clean = []
    for s in found:
        a = max(0.0, s["start"])
        b = min(total_duration, s["end"]) if total_duration > 0 else s["end"]
        if b - a >= 0.5:
            clean.append({"start": a, "end": b, "category": s["category"]})
    return _merge_segments(clean)


async def remove_sponsor_segments(input_path: Union[str, Path], output_path: Union[str, Path],
                                  segments: List[Dict],
                                  progress_cb: ProgressCb = None) -> Tuple[str, float]:
    input_path, output_path = Path(input_path), Path(output_path)
    info = await get_media_info(input_path)
    dur = info["duration"]
    if not segments or dur <= 0:
        shutil.copyfile(input_path, output_path)
        return str(output_path), 0.0

    keeps, cur = [], 0.0
    for s in segments:
        if s["start"] > cur:
            keeps.append((cur, s["start"]))
        cur = max(cur, s["end"])
    if cur < dur:
        keeps.append((cur, dur))
    keeps = [(a, b) for a, b in keeps if b - a > 0.3]
    if not keeps:
        raise RuntimeError("После удаления рекламы от видео ничего не осталось")

    removed = dur - sum(b - a for a, b in keeps)
    w, h, fps = _even(info["width"]), _even(info["height"]), info["fps"]
    work = output_path.parent / (output_path.stem + "_sb")
    work.mkdir(parents=True, exist_ok=True)
    total_keep = sum(b - a for a, b in keeps)
    done = 0.0
    pieces = []
    try:
        for i, (a, b) in enumerate(keeps):
            piece = work / f"keep_{i:03d}.mp4"

            async def cb(p, _a=a, _b=b, _done=done):
                await _maybe_await(progress_cb, (_done + p * (_b - _a)) / total_keep)

            await encode_segment(input_path, piece, a, b, w, h, fps,
                                 info["has_audio"], info["has_audio"],
                                 info["sample_rate"], info["channels"],
                                 "fast", 20, cb)
            pieces.append(piece)
            done += b - a
        await _concat_copy(pieces, output_path, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return str(output_path), removed


# =============================================================================
# 2.4 Нарезка на части
# =============================================================================
async def split_video_into_parts(input_path: Union[str, Path], output_dir: Union[str, Path],
                                 num_parts: int,
                                 on_part_done: Optional[Callable] = None,
                                 progress_cb: ProgressCb = None) -> List[Dict]:
    input_path, output_dir = Path(input_path), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    info = await get_media_info(input_path)
    dur = info["duration"]
    if dur <= 0:
        raise RuntimeError("Не удалось определить длительность видео")
    num_parts = max(1, min(int(num_parts), 50))
    seg_len = dur / num_parts
    result: List[Dict] = []

    if num_parts == 1:
        dst = output_dir / "part_001.mp4"
        shutil.copyfile(input_path, dst)
        item = {"index": 1, "path": str(dst), "start": 0.0, "end": dur, "duration": dur}
        result.append(item)
        await _maybe_await(on_part_done, 1, 1, item)
        return result

    w, h = _even(info["width"]), _even(info["height"])
    for i in range(num_parts):
        a = i * seg_len
        b = dur if i == num_parts - 1 else (i + 1) * seg_len
        dst = output_dir / f"part_{i + 1:03d}.mp4"

        async def cb(p, _i=i):
            await _maybe_await(progress_cb, (_i + p) / num_parts)

        await encode_segment(input_path, dst, a, b, w, h, info["fps"],
                             info["has_audio"], info["has_audio"],
                             info["sample_rate"], info["channels"],
                             "ultrafast", 18, cb)
        item = {"index": i + 1, "path": str(dst), "start": a, "end": b, "duration": b - a}
        result.append(item)
        await _maybe_await(on_part_done, i + 1, num_parts, item)
    return result


# =============================================================================
# Раскладка: 9:16 с размытым фоном / ASMR-видео снизу
# =============================================================================
def _blur_fit(src: str, w: int, h: int, out: str) -> str:
    return (f"{src}split[{out}_bg][{out}_fg];"
            f"[{out}_bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"boxblur=20:5,eq=brightness=-0.08[{out}_b];"
            f"[{out}_fg]scale={w}:{h}:force_original_aspect_ratio=decrease[{out}_s];"
            f"[{out}_b][{out}_s]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")


async def apply_layout(input_path: Union[str, Path], output_path: Union[str, Path],
                       aspect: str = "original",
                       asmr_path: Optional[Union[str, Path]] = None,
                       progress_cb: ProgressCb = None) -> Dict:
    """
    aspect: "original" | "3:4" | "9:16".
    * asmr_path задан -> split-screen в формате aspect (для "original" берётся 3:4):
      основное видео сверху с размытым фоном, ASMR снизу — вписывается целиком
      без обрезки, тоже с размытым фоном (зациклено, без звука).
    * иначе aspect != original и пропорции отличаются -> вписываем с размытым фоном.
    * иначе -> без изменений.
    Возвращает {"path", "layout", "aspect"}.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    info = await get_media_info(input_path)
    dur, fps = info["duration"], info["fps"]
    src_w, src_h = info["width"], info["height"]

    def audio_args():
        if info["has_audio"]:
            return ["-map", "0:a:0", *_aenc(info["sample_rate"], info["channels"])]
        return ["-an"]

    if asmr_path and Path(asmr_path).exists():
        asp = aspect if aspect in ASPECTS else "3:4"
        W, H = target_size(asp, src_w, src_h)
        top_h = _even(H * ASMR_TOP_SHARE)
        bot_h = H - top_h
        args = ["-i", str(input_path), "-stream_loop", "-1", "-i", str(asmr_path)]
        fc = (_blur_fit("[0:v]", W, top_h, "top") + ";"
              # ASMR уменьшается целиком (без обрезки), поля заполняются его же размытой копией
              + _blur_fit(f"[1:v]fps={fps:.3f},", W, bot_h, "bot") + ";"
              f"[top][bot]vstack=inputs=2,setsar=1,format=yuv420p[v]")
        args += ["-filter_complex", fc, "-map", "[v]", *audio_args(),
                 "-r", f"{fps:.3f}", *_venc("fast", 20), "-t", f"{dur:.3f}", str(output_path)]
        await run_ffmpeg(args, dur, progress_cb)
        return {"path": str(output_path), "layout": "asmr", "aspect": asp}

    if aspect in ASPECTS and src_w and src_h:
        W, H = target_size(aspect, src_w, src_h)
        if abs(src_w / src_h - W / H) > 0.02:  # пропорции уже подходят — не трогаем
            fc = _blur_fit("[0:v]", W, H, "v")
            args = ["-i", str(input_path), "-filter_complex", fc, "-map", "[v]", *audio_args(),
                    "-r", f"{fps:.3f}", *_venc("fast", 20), str(output_path)]
            await run_ffmpeg(args, dur, progress_cb)
            return {"path": str(output_path), "layout": "fit", "aspect": aspect}
        return {"path": str(input_path), "layout": "fit", "aspect": aspect}

    return {"path": str(input_path), "layout": "original", "aspect": "original"}


# =============================================================================
# Проверка баннера (>= 25% экрана)
# =============================================================================
def _opaque_ratio(img: Image.Image) -> float:
    img = img.convert("RGBA")
    img.thumbnail((256, 256))
    hist = img.getchannel("A").histogram()
    total = sum(hist) or 1
    return sum(hist[17:]) / total  # пиксели с альфой > ~6%


def banner_kind(path: Union[str, Path]) -> Optional[str]:
    """Принимает путь ИЛИ голое расширение ('.mp4'). Path('.mp4').suffix == '' — учитываем."""
    s = str(path).strip().lower()
    ext = s if (s.startswith(".") and "/" not in s and "\\" not in s and s.count(".") == 1) \
        else Path(s).suffix
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_BANNER_EXTS:
        return "video"
    return None


async def analyze_banner(banner_path: Union[str, Path], work_dir: Union[str, Path]) -> Dict:
    """
    Вернуть {kind, width, height, opaque_ratio, duration, has_audio, info}.
    opaque_ratio — доля непрозрачных пикселей самого баннера (0..1).
    """
    banner_path, work_dir = Path(banner_path), Path(work_dir)
    kind = banner_kind(banner_path)
    if not kind:
        raise ValueError("Неподдерживаемый формат баннера")
    info = await get_media_info(banner_path)

    if kind == "image":
        with Image.open(banner_path) as im:
            w, h = im.size
            ratio = _opaque_ratio(im)
        return {"kind": "image", "width": w, "height": h, "opaque_ratio": ratio,
                "duration": 0.0, "has_audio": False, "info": info}

    if not info["width"]:
        raise ValueError("Не удалось прочитать видео-баннер")
    dur = info["duration"]
    stamps = [dur * k for k in (0.1, 0.5, 0.9)] if dur > 0.3 else [0.0]
    ratios = []
    for i, t in enumerate(stamps):
        fr = work_dir / f"banner_probe_{i}.png"
        try:
            await run_ffmpeg([*_alpha_decoder_args(info), "-ss", f"{t:.3f}",
                              "-i", str(banner_path), "-frames:v", "1",
                              "-pix_fmt", "rgba", str(fr)])
            if fr.exists():
                with Image.open(fr) as im:
                    ratios.append(_opaque_ratio(im))
        except FFmpegError as e:
            log.debug("probe frame failed: %s", e)
        finally:
            fr.unlink(missing_ok=True)
    if not ratios:
        raise ValueError("Не удалось извлечь кадры из видео-баннера")
    return {"kind": "video", "width": info["width"], "height": info["height"],
            "opaque_ratio": sum(ratios) / len(ratios),
            "duration": dur if dur > 0.1 else 0.0,
            "has_audio": info["has_audio"], "info": info}


def banner_screen_coverage(meta: Dict, screen_w: int, screen_h: int) -> float:
    """
    Какую долю экрана реально займёт баннер: вписываем его в кадр с сохранением
    пропорций, умножаем на долю его непрозрачных пикселей.
    """
    bw, bh = meta["width"], meta["height"]
    if not (bw and bh and screen_w and screen_h):
        return 0.0
    fit = min(screen_w / bw, screen_h / bh)
    area = (bw * fit) * (bh * fit) / (screen_w * screen_h)
    return area * meta["opaque_ratio"]


def check_banner_coverage(meta: Dict, screen_w: int = 0, screen_h: int = 0) -> Tuple[bool, float, str]:
    """
    Без размеров экрана проверяется только сам баннер (доля непрозрачного).
    С размерами — реальное покрытие кадра конкретного видео.
    """
    if screen_w and screen_h:
        cov = banner_screen_coverage(meta, screen_w, screen_h)
        where = f"кадра {screen_w}x{screen_h}"
    else:
        cov = meta["opaque_ratio"]
        where = "площади баннера"
    ok = cov >= MIN_BANNER_SCREEN_RATIO
    msg = (f"Баннер занимает {cov * 100:.0f}% {where} "
           f"(минимум {MIN_BANNER_SCREEN_RATIO * 100:.0f}%)")
    return ok, cov, msg


# =============================================================================
# 2.6 Вставка баннера в середину
# =============================================================================
async def insert_banner_at_middle(video_path: Union[str, Path], banner_path: Union[str, Path],
                                  output_path: Union[str, Path], ad_duration: float = 3.0,
                                  banner_meta: Optional[Dict] = None,
                                  progress_cb: ProgressCb = None) -> Dict:
    """
    part1 (0..mid) + ad_segment (стоп-кадр + баннер) + part2 (mid..end) -> concat.
    Возвращает {"path", "inserted", "mid_time", "ad_duration", "coverage", "reason"}.
    """
    video_path, banner_path, output_path = Path(video_path), Path(banner_path), Path(output_path)
    work = output_path.parent / (output_path.stem + "_ad")
    work.mkdir(parents=True, exist_ok=True)

    try:
        info = await get_media_info(video_path)
        dur = info["duration"]
        if dur < 1.0:
            raise RuntimeError("Видео слишком короткое для вставки баннера")
        W, H, fps = _even(info["width"]), _even(info["height"]), info["fps"]

        meta = banner_meta or await analyze_banner(banner_path, work)
        ok, cov, msg = check_banner_coverage(meta, W, H)
        if not ok:
            shutil.copyfile(video_path, output_path)
            return {"path": str(output_path), "inserted": False, "mid_time": 0.0,
                    "ad_duration": 0.0, "coverage": cov, "reason": msg}

        is_video_banner = meta["kind"] == "video" and meta["duration"] > 0.1
        ad_len = meta["duration"] if is_video_banner else float(ad_duration)
        ad_len = max(0.5, min(ad_len, 60.0))
        banner_has_audio = is_video_banner and meta["has_audio"]

        want_audio = info["has_audio"] or banner_has_audio
        sr = info["sample_rate"] if info["has_audio"] else meta["info"].get("sample_rate", 44100)
        ch = info["channels"] if info["has_audio"] else "stereo"

        mid = dur / 2.0
        total = dur + ad_len
        part1, ad_seg, part2 = work / "part1.mp4", work / "ad_segment.mp4", work / "part2.mp4"
        freeze = work / "freeze_frame.png"

        # 1. стоп-кадр ровно из середины
        for t in (mid, max(0.0, mid - 0.1), max(0.0, mid - 0.5)):
            try:
                await run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(video_path), "-frames:v", "1",
                                  "-vf", f"scale={W}:{H},setsar=1", str(freeze)])
                if freeze.exists() and freeze.stat().st_size > 0:
                    break
            except FFmpegError:
                continue
        if not freeze.exists():
            raise RuntimeError("Не удалось извлечь стоп-кадр из середины видео")

        async def stage(offset, length):
            async def cb(p):
                await _maybe_await(progress_cb, (offset + p * length) / total)
            return cb

        # 2. первая половина
        await encode_segment(video_path, part1, 0.0, mid, W, H, fps, info["has_audio"],
                             want_audio, sr, ch, "fast", 20, await stage(0, mid))

        # 3. рекламный сегмент
        args = ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{ad_len:.3f}", "-i", str(freeze)]
        if is_video_banner:
            args += [*_alpha_decoder_args(meta["info"]), "-i", str(banner_path)]
        else:
            args += ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{ad_len:.3f}",
                     "-i", str(banner_path)]
        audio_map = None
        afilter = ""
        if want_audio:
            lay = _audio_layout(ch)
            if banner_has_audio:
                afilter = (f";[1:a]aresample={sr},aformat=sample_rates={sr}:channel_layouts={lay},"
                           f"apad,atrim=0:{ad_len:.3f},asetpts=PTS-STARTPTS[a]")
                audio_map = "[a]"
            else:
                args += ["-f", "lavfi", "-t", f"{ad_len:.3f}", "-i", f"anullsrc=r={sr}:cl={lay}"]
                audio_map = "2:a:0"
        fc = (f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,format=rgba,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,setsar=1[bn];"
              f"[0:v]scale={W}:{H},setsar=1,format=rgba[bg];"
              f"[bg][bn]overlay=0:0:format=auto:eof_action=pass,fps={fps:.3f},"
              f"trim=duration={ad_len:.3f},setpts=PTS-STARTPTS,format=yuv420p[v]" + afilter)
        args += ["-filter_complex", fc, "-map", "[v]"]
        if audio_map:
            args += ["-map", audio_map, *_aenc(sr, ch)]
        else:
            args += ["-an"]
        args += ["-r", f"{fps:.3f}", *_venc("fast", 20), "-t", f"{ad_len:.3f}", str(ad_seg)]
        await run_ffmpeg(args, ad_len, await stage(mid, ad_len))

        # 4. вторая половина
        await encode_segment(video_path, part2, mid, dur, W, H, fps, info["has_audio"],
                             want_audio, sr, ch, "fast", 20, await stage(mid + ad_len, dur - mid))

        # 5. склейка
        await _concat_copy([part1, ad_seg, part2], output_path, work)
        return {"path": str(output_path), "inserted": True, "mid_time": mid,
                "ad_duration": ad_len, "coverage": cov, "reason": ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# =============================================================================
# Превью для Telegram
# =============================================================================
async def make_thumbnail(video_path: Union[str, Path], thumb_path: Union[str, Path]) -> Optional[str]:
    try:
        info = await get_media_info(video_path)
        t = min(1.0, info["duration"] / 3) if info["duration"] else 0
        await run_ffmpeg(["-ss", f"{t:.2f}", "-i", str(video_path), "-frames:v", "1",
                          "-vf", "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'",
                          "-q:v", "5", str(thumb_path)])
        return str(thumb_path) if Path(thumb_path).exists() else None
    except Exception:
        return None