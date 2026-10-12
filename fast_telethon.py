"""
fast_telethon.py — параллельная загрузка/выгрузка больших файлов для Telethon.

Telethon по умолчанию качает файл в ОДНО соединение, по одному чанку.
Здесь файл делится на чанки и гоняется через несколько MTProto-соединений
с тем же DC одновременно — это и даёт ускорение в разы на файлах 100 МБ–2 ГБ.

Ускорение самого AES-IGE в Telethon делает пакет `cryptg` (нативный C):
Telethon подхватывает его автоматически, если он установлен.
(PyCryptodome режим IGE не поддерживает, поэтому патчить через него нельзя.)

Основано на открытой реализации FastTelethon (tulir / mautrix-telegram, MIT).
"""
import asyncio
import hashlib
import inspect
import logging
import math
import os
from collections import defaultdict
from typing import AsyncGenerator, Awaitable, BinaryIO, Callable, List, Optional, Union

from telethon import TelegramClient, helpers, utils
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import (
    GetFileRequest,
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import (
    Document,
    InputDocumentFileLocation,
    InputFile,
    InputFileBig,
    InputFileLocation,
    InputPeerPhotoFileLocation,
    InputPhotoFileLocation,
    TypeInputFile,
)

log = logging.getLogger("fast_telethon")

try:
    import cryptg  # noqa: F401
    CRYPTG_OK = True
except Exception:
    CRYPTG_OK = False

TypeLocation = Union[
    Document,
    InputDocumentFileLocation,
    InputPeerPhotoFileLocation,
    InputFileLocation,
    InputPhotoFileLocation,
]
ProgressCb = Optional[Callable[[int, int], Union[Awaitable[None], None]]]


async def _call_progress(cb: ProgressCb, cur: int, total: int) -> None:
    if cb is None:
        return
    try:
        r = cb(cur, total)
        if inspect.isawaitable(r):
            await r
    except Exception as e:  # прогресс никогда не должен ронять передачу
        log.debug("progress callback error: %s", e)


class DownloadSender:
    def __init__(self, client: TelegramClient, sender: MTProtoSender,
                 file: TypeLocation, offset: int, limit: int, stride: int, count: int):
        self.sender = sender
        self.client = client
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        result = await self.client._call(self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self) -> Awaitable[None]:
        return self.sender.disconnect()


class UploadSender:
    def __init__(self, client: TelegramClient, sender: MTProtoSender, file_id: int,
                 part_count: int, big: bool, index: int, stride: int, loop):
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous: Optional[asyncio.Task] = None
        self.loop = loop

    async def next(self, data: bytes) -> None:
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        await self.client._call(self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        return await self.sender.disconnect()


class ParallelTransferrer:
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None):
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.auth_key = (None if dc_id and self.client.session.dc_id != dc_id
                         else self.client.session.auth_key)
        self.senders: Optional[List[Union[DownloadSender, UploadSender]]] = None
        self.upload_ticker = 0

    async def _cleanup(self) -> None:
        await asyncio.gather(*[s.disconnect() for s in self.senders])
        self.senders = None

    @staticmethod
    def _get_connection_count(file_size: int, max_count: int = 20,
                              full_size: int = 100 * 1024 * 1024) -> int:
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=self.client._log, proxy=self.client._proxy))
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes)
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(self, file: TypeLocation, index: int, part_size: int,
                                      stride: int, part_count: int) -> DownloadSender:
        return DownloadSender(self.client, await self._create_sender(), file,
                              index * part_size, part_size, stride, part_count)

    async def _create_upload_sender(self, file_id: int, part_count: int, big: bool,
                                    index: int, stride: int) -> UploadSender:
        return UploadSender(self.client, await self._create_sender(), file_id,
                            part_count, big, index, stride, loop=self.loop)

    async def _init_download(self, connections: int, file: TypeLocation,
                             part_count: int, part_size: int) -> None:
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # первый sender создаём отдельно — он экспортирует авторизацию
        self.senders = [await self._create_download_sender(
            file, 0, part_size, connections * part_size, get_part_count())]
        self.senders += await asyncio.gather(*[
            self._create_download_sender(file, i, part_size,
                                         connections * part_size, get_part_count())
            for i in range(1, connections)
        ])

    async def _init_upload(self, connections: int, file_id: int,
                           part_count: int, big: bool) -> None:
        self.senders = [await self._create_upload_sender(file_id, part_count, big, 0, connections)]
        self.senders += await asyncio.gather(*[
            self._create_upload_sender(file_id, part_count, big, i, connections)
            for i in range(1, connections)
        ])

    async def init_upload(self, file_id: int, file_size: int,
                          part_size_kb: Optional[float] = None,
                          connection_count: Optional[int] = None):
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        is_large = file_size > 10 * 1024 * 1024
        await self._init_upload(connection_count, file_id, part_count, is_large)
        return part_size, part_count, is_large

    async def upload(self, part: bytes) -> None:
        await self.senders[self.upload_ticker].next(part)
        self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)

    async def finish_upload(self) -> None:
        await self._cleanup()

    async def download(self, file: TypeLocation, file_size: int,
                       part_size_kb: Optional[float] = None,
                       connection_count: Optional[int] = None) -> AsyncGenerator[bytes, None]:
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = math.ceil(file_size / part_size)
        await self._init_download(connection_count, file, part_count, part_size)

        part = 0
        while part < part_count:
            tasks = [self.loop.create_task(s.next()) for s in self.senders]
            for task in tasks:
                data = await task
                if not data:
                    break
                yield data
                part += 1
        await self._cleanup()


parallel_transfer_locks: defaultdict = defaultdict(lambda: asyncio.Lock())


async def download_file(client: TelegramClient, location: TypeLocation, out: BinaryIO,
                        progress_callback: ProgressCb = None) -> BinaryIO:
    """Параллельно скачать документ Telegram в открытый бинарный файл."""
    size = location.size
    dc_id, location = utils.get_input_location(location)
    downloader = ParallelTransferrer(client, dc_id)
    downloaded = downloader.download(location, size)
    done = 0
    async for chunk in downloaded:
        out.write(chunk)
        done += len(chunk)
        await _call_progress(progress_callback, min(done, size), size)
    return out


async def _internal_upload(client: TelegramClient, file: BinaryIO,
                           progress_callback: ProgressCb = None):
    file_id = helpers.generate_random_long()
    file_size = os.path.getsize(file.name)
    hash_md5 = hashlib.md5()
    uploader = ParallelTransferrer(client)
    part_size, part_count, is_large = await uploader.init_upload(file_id, file_size)
    buffer = bytearray()
    for data in _stream_file(file):
        await _call_progress(progress_callback, file.tell(), file_size)
        if not is_large:
            hash_md5.update(data)
        if len(buffer) == 0 and len(data) == part_size:
            await uploader.upload(data)
            continue
        new_len = len(buffer) + len(data)
        if new_len >= part_size:
            cutoff = part_size - len(buffer)
            buffer.extend(data[:cutoff])
            await uploader.upload(bytes(buffer))
            buffer.clear()
            buffer.extend(data[cutoff:])
        else:
            buffer.extend(data)
    if len(buffer) > 0:
        await uploader.upload(bytes(buffer))
    await uploader.finish_upload()
    await _call_progress(progress_callback, file_size, file_size)
    if is_large:
        return InputFileBig(file_id, part_count, os.path.basename(file.name)), file_size
    return InputFile(file_id, part_count, os.path.basename(file.name),
                     hash_md5.hexdigest()), file_size


def _stream_file(file_to_stream: BinaryIO, chunk_size: int = 1024 * 1024):
    while True:
        data = file_to_stream.read(chunk_size)
        if not data:
            break
        yield data


async def upload_file(client: TelegramClient, file: BinaryIO,
                      progress_callback: ProgressCb = None) -> TypeInputFile:
    """Параллельно загрузить открытый бинарный файл в Telegram. Возвращает InputFile."""
    return (await _internal_upload(client, file, progress_callback))[0]


async def fast_download(client: TelegramClient, document: Document, path: str,
                        progress_callback: ProgressCb = None) -> str:
    """Скачать документ в path. При сбое — откат на стандартный метод Telethon."""
    try:
        with open(path, "wb") as f:
            await download_file(client, document, f, progress_callback)
        return path
    except Exception as e:
        log.warning("Параллельная загрузка не удалась (%s), фолбэк на стандартную", e)
        return await client.download_media(document, file=path,
                                           progress_callback=progress_callback)


async def fast_upload(client: TelegramClient, path: str,
                      progress_callback: ProgressCb = None):
    """Загрузить файл с диска. При сбое — откат на стандартный upload_file."""
    try:
        with open(path, "rb") as f:
            return await upload_file(client, f, progress_callback)
    except Exception as e:
        log.warning("Параллельная выгрузка не удалась (%s), фолбэк на стандартную", e)
        return await client.upload_file(path, progress_callback=progress_callback)