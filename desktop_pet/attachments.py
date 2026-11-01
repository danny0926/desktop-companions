"""Private, durable user images. Pillow is imported only during explicit loading."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import io
import math
from pathlib import Path
import re
import struct
import threading
from urllib.parse import unquote, urlsplit

from desktop_pet.contracts import ManualImage
from desktop_pet.markdown_chat import image_references


MAX_IMAGES = 4
MAX_FILE_BYTES = 12_000_000
MAX_CLIPBOARD_IMAGE_BYTES = 160_004_096
MAX_SOURCE_PIXELS = 40_000_000
MAX_EDGE = 1600
THUMBNAIL_SIZE = (300, 180)
IMAGE_LIMIT_HINT = "每次最多 4 張；PNG／JPEG／GIF，原檔每張 12 MB、4000 萬像素內，傳送時縮至 1600 px。僅載入你選擇或明確引用的本機圖片。"


def _dib_as_bmp(payload: bytes) -> bytes:
    """Wrap a bounded Windows DIB in a BMP file header for Pillow."""
    if len(payload) < 40:
        raise ValueError("剪貼簿點陣圖標頭無效；草稿已保留。")
    header_size, width, height, planes, bpp, compression, _image_size = struct.unpack_from("<IiiHHII", payload)
    if header_size not in {40, 52, 56, 108, 124} or len(payload) < header_size:
        raise ValueError("剪貼簿點陣圖標頭不支援；草稿已保留。")
    if not 1 <= width <= 40_000 or not 1 <= abs(height) <= 40_000 or planes != 1 or bpp not in {1, 4, 8, 16, 24, 32}:
        raise ValueError("剪貼簿點陣圖尺寸無效；草稿已保留。")
    if width * abs(height) > MAX_SOURCE_PIXELS:
        raise ValueError("剪貼簿圖片超過 4000 萬像素；草稿已保留。")
    if compression not in {0, 3, 6}:
        raise ValueError("剪貼簿點陣圖壓縮格式不支援；草稿已保留。")
    offset = header_size
    if compression == 3 and header_size == 40:
        offset += 12
    elif compression == 6 and header_size == 40:
        offset += 16
    if bpp <= 8:
        colors_used = struct.unpack_from("<I", payload, 32)[0]
        offset += (colors_used or (1 << bpp)) * 4
    if offset > len(payload):
        raise ValueError("剪貼簿點陣圖色彩表不完整；草稿已保留。")
    return b"BM" + struct.pack("<IHHI", 14 + len(payload), 0, 0, 14 + offset) + payload


def _image_id(reference: str) -> str:
    image_id = reference.removeprefix("attachment:")
    if not re.fullmatch(r"[a-f0-9]{64}", image_id):
        raise ValueError("圖片附件編號無效；草稿已保留。")
    return image_id


def _local_path(source: str) -> Path:
    if source.startswith("file:"):
        url = urlsplit(source)
        if url.netloc not in {"", "localhost"}:
            raise ValueError("不載入網路磁碟圖片；請用「圖片」選擇本機檔案，草稿已保留。")
        source = unquote(url.path)
        if re.match(r"^/[A-Za-z]:/", source):
            source = source[1:]
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source) and not re.match(r"^[A-Za-z]:[/\\]", source):
        raise ValueError("圖片目前支援本機檔案；請先下載網址圖片，再按「圖片」選取。草稿已保留。")
    else:
        source = unquote(source)
    if not source or source.startswith(("\\\\", "//")):
        raise ValueError("請選擇本機圖片檔案；草稿已保留。")
    return Path(source).expanduser()


class AttachmentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._thumbnail_cache: OrderedDict[tuple[int, str], object] = OrderedDict()

    def _write(self, path: Path, payload: bytes):
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.read_bytes() == payload:
                return
            # All paths are content-derived inside this character's private
            # attachment folder. No source names or source metadata are saved.
            part = path.with_suffix(".part")
            part.write_bytes(payload)
            part.replace(path)

    def import_file(self, path: str | Path) -> ManualImage:
        path = Path(path)
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("圖片不存在或超過 12 MB；草稿已保留。")
            with path.open("rb") as stream:
                payload = stream.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                raise ValueError("圖片超過 12 MB；草稿已保留。")
            return self.import_bytes(payload)
        except OSError:
            raise ValueError("無法讀取或保存圖片；請確認本機檔案與權限，草稿已保留。") from None

    def import_bytes(self, payload: bytes, *, image_format: str | None = None) -> ManualImage:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("剪貼簿圖片資料無效；草稿已保留。")
        if image_format not in {None, "png", "dib", "dibv5"}:
            raise ValueError("剪貼簿圖片格式不支援；草稿已保留。")
        is_dib = image_format in {"dib", "dibv5"}
        if len(payload) > (MAX_CLIPBOARD_IMAGE_BYTES if is_dib else MAX_FILE_BYTES):
            raise ValueError("圖片超過可匯入大小限制；草稿已保留。")
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError:
            if is_dib:
                raise ValueError("匯入剪貼簿點陣圖需要 Pillow；草稿已保留。") from None
            return self._import_png_without_pillow(payload)
        try:
            data = _dib_as_bmp(payload) if is_dib else payload
            with Image.open(io.BytesIO(data)) as original:
                accepted = {"PNG"} if image_format == "png" else {"PNG", "JPEG", "GIF", "BMP"}
                if original.format not in accepted:
                    raise ValueError("圖片格式不支援；草稿已保留。")
                if original.width * original.height > MAX_SOURCE_PIXELS:
                    raise ValueError("圖片超過 4000 萬像素；草稿已保留。")
                original.seek(0)
                image = ImageOps.exif_transpose(original).convert("RGBA")
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                normalized = io.BytesIO()
                image.save(normalized, format="PNG", optimize=False)
                png = normalized.getvalue()
                if len(png) > 8_000_000:
                    raise ValueError("正規化後圖片超過 8 MB；草稿已保留。")
                image_id = hashlib.sha256(png).hexdigest()
                attachment = ManualImage(png_bytes=png, image_id=image_id, width=image.width, height=image.height)
                thumb = image.copy()
                thumb.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                thumbnail = io.BytesIO()
                thumb.save(thumbnail, format="PNG")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, struct.error):
            raise ValueError("圖片損壞或尺寸過大；草稿已保留。") from None
        self._write(self.root / f"{image_id}.png", png)
        self._write(self.root / f"{image_id}.thumb.png", thumbnail.getvalue())
        return attachment

    def _import_png_without_pillow(self, payload: bytes) -> ManualImage:
        if len(payload) < 33 or payload[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("JPEG／GIF 或縮圖需要 Pillow；請安裝 desktop_pet/requirements-chat.txt。草稿已保留。")
        width, height = struct.unpack(">II", payload[16:24])
        try:
            ManualImage(png_bytes=payload, image_id=hashlib.sha256(payload).hexdigest(), width=width, height=height)
        except ValueError:
            raise ValueError("這張圖片需要先縮小；請安裝 Pillow 或使用 1600 px、8 MB 內的 PNG。草稿已保留。") from None
        # Verify structure/CRC with stdlib. Tk will decode pixels on display.
        import zlib
        position, ended = 8, False
        normalized, compressed = bytearray(payload[:8]), bytearray()
        bit_depth, color_type, _, _, interlace = payload[24:29]
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
        if bit_depth != 8 or interlace or not channels:
            raise ValueError("這種 PNG 編碼需要 Pillow；請安裝 desktop_pet/requirements-chat.txt。草稿已保留。")
        while position + 12 <= len(payload):
            length = struct.unpack(">I", payload[position:position + 4])[0]
            chunk = payload[position + 4:position + 8 + length]
            end = position + 12 + length
            if end > len(payload) or zlib.crc32(chunk) != int.from_bytes(payload[end - 4:end], "big"):
                raise ValueError("PNG 圖片損壞；草稿已保留。")
            if chunk[:4] in {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}:
                normalized.extend(payload[position:end])
            if chunk[:4] == b"IDAT":
                compressed.extend(chunk[4:])
            position = end
            if chunk[:4] == b"IEND":
                ended = True
                break
        if not ended:
            raise ValueError("PNG 圖片不完整；草稿已保留。")
        try:
            expected = (width * channels + 1) * height
            decoder = zlib.decompressobj()
            pixels = decoder.decompress(compressed, expected + 1)
            if (len(pixels) != expected or not decoder.eof
                    or any(pixels[row * (width * channels + 1)] > 4 for row in range(height))):
                raise ValueError
        except (ValueError, zlib.error):
            raise ValueError("PNG 圖片像素損壞；草稿已保留。") from None
        png = bytes(normalized)
        image_id = hashlib.sha256(png).hexdigest()
        image = ManualImage(png_bytes=png, image_id=image_id, width=width, height=height)
        self._write(self.root / f"{image_id}.png", png)
        return image

    def load(self, reference: str) -> ManualImage:
        image_id = _image_id(reference)
        try:
            png = (self.root / f"{image_id}.png").read_bytes()
            width, height = struct.unpack(">II", png[16:24])
            return ManualImage(png_bytes=png, image_id=image_id, width=width, height=height)
        except (OSError, ValueError, struct.error):
            raise ValueError("圖片附件遺失或損壞；請按「圖片」重新選擇，草稿已保留。") from None

    @staticmethod
    def referenced_ids(source: str) -> list[str]:
        return list(dict.fromkeys(_image_id(ref.source) for ref in image_references(source)
                                  if ref.source.startswith("attachment:")))

    def prepare_markdown(self, source: str) -> tuple[str, tuple[ManualImage, ...]]:
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        env: dict = {}
        references = image_references(source, env=env)
        if len(references) > MAX_IMAGES:
            raise ValueError("每則訊息最多 4 張圖片；請移除多餘標記，草稿已保留。")
        images, changes, resolved = [], [], {}
        for ref in references:
            if ref.source not in resolved:
                resolved[ref.source] = (self.load(ref.source) if ref.source.startswith("attachment:")
                                        else self.import_file(_local_path(ref.source)))
            image = resolved[ref.source]
            images.append(image)
            # Preserve visible alt text without allowing it to create another
            # image/link. Refs are ordered exactly as images in the request.
            alt = re.sub(r"([\\\[\]])", r"\\\1", ref.alt.replace("\n", " "))
            changes.append((ref.start, ref.end, f"![{alt}]({image.reference})"))
        # Replace used reference definitions as well, so source paths never
        # enter model prompts, saved turns or event records.
        line_offsets, total = [0], 0
        for line in source.splitlines(keepends=True):
            total += len(line)
            line_offsets.append(total)
        for label, definition in env.get("references", {}).items():
            image = resolved.get(definition["href"])
            if image and "map" in definition:
                start, end = definition["map"]
                changes.append((line_offsets[start], line_offsets[end], f"[{label}]: {image.reference}\n"))
        for start, end, replacement in sorted(changes, reverse=True):
            source = source[:start] + replacement + source[end:]
        return source, tuple(images)

    def thumbnail(self, reference: str, master):
        import tkinter as tk
        image_id = _image_id(reference)
        key = (id(master.tk), image_id)
        if key in self._thumbnail_cache:
            self._thumbnail_cache.move_to_end(key)
            return self._thumbnail_cache[key]
        thumbnail = self.root / f"{image_id}.thumb.png"
        if thumbnail.is_file():
            photo = tk.PhotoImage(master=master, file=str(thumbnail))
        else:
            image = self.load(reference)
            photo = tk.PhotoImage(master=master, data=image.png_bytes, format="png")
            factor = math.ceil(max(photo.width() / THUMBNAIL_SIZE[0], photo.height() / THUMBNAIL_SIZE[1]))
            if factor > 1:
                photo = photo.subsample(factor, factor)
        self._thumbnail_cache[key] = photo
        while len(self._thumbnail_cache) > 24:
            self._thumbnail_cache.popitem(last=False)
        return photo
