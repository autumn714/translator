"""Images (PNG/JPG/WEBP): the vision model reads and translates the text -> DOCX.

The Word file holds the translation (Markdown from the model rendered as
headings, lists and tables) followed by the original image for reference.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from translator_app.documents.base import (
    MSG_BROKEN,
    DocumentError,
    HandlerContext,
    HandlerResult,
)
from translator_app.documents.docx_writer import DocxWriter
from translator_app.documents.ooxml import lang_tag

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
UNIT_CHARS = 1500                      # progress weight of the vision-model call
EMBED_MAX_SIDE = 2000                  # original image copy in the DOCX
MAX_PIXELS = 80_000_000

MSG_IMAGE_TOO_BIG = "이미지 해상도가 너무 큽니다."
MSG_IMAGE_NO_TEXT = "이미지에서 글자를 찾지 못했습니다."


@dataclass
class ImageInfo:
    width: int
    height: int
    embed: bytes                        # PNG or JPEG for the DOCX
    embed_ext: str                      # "png" | "jpeg"


EXIF_ORIENTATION = 0x0112


def _orientation(im) -> int:
    try:
        return int(im.getexif().get(EXIF_ORIENTATION, 1) or 1)
    except Exception:  # noqa: BLE001 - broken EXIF block: treat as upright
        return 1


def inspect_image(data: bytes) -> ImageInfo:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w * h > MAX_PIXELS:
                raise DocumentError(MSG_IMAGE_TOO_BIG)
            im.load()
            fmt = (im.format or "").upper()
            rotated = _orientation(im) not in (0, 1)
            if fmt in ("PNG", "JPEG") and max(w, h) <= EMBED_MAX_SIDE and not rotated:
                return ImageInfo(w, h, data, "png" if fmt == "PNG" else "jpeg")
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            # phone photos are stored sideways with an EXIF orientation tag; the copy
            # in the Word file has no EXIF, so the pixels themselves must be upright
            img = ImageOps.exif_transpose(im) if rotated else im
            if max(img.size) > EMBED_MAX_SIDE:
                img = img.copy() if img is im else img
                img.thumbnail((EMBED_MAX_SIDE, EMBED_MAX_SIDE))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P") or fmt == "PNG":
                img.save(buf, "PNG", optimize=True)
                ext = "png"
            else:
                img.convert("RGB").save(buf, "JPEG", quality=88)
                ext = "jpeg"
            return ImageInfo(img.size[0], img.size[1], buf.getvalue(), ext)
    except DocumentError:
        raise
    except Image.DecompressionBombError as exc:
        raise DocumentError(MSG_IMAGE_TOO_BIG) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise DocumentError(MSG_BROKEN) from exc


def build_docx(text: str, info: ImageInfo, lang: str | None) -> bytes:
    w = DocxWriter(lang=lang)
    w.markdown(text)
    w.paragraph("")
    w.paragraph("원본 이미지", style="Caption")
    w.image(info.embed, info.embed_ext, info.width, info.height)
    return w.to_bytes()


class ImageHandler:
    output_ext = ".docx"

    async def run(self, ctx: HandlerContext) -> HandlerResult:
        ctx.set_status("extracting")
        data = await asyncio.to_thread(ctx.input_path.read_bytes)
        info = await asyncio.to_thread(inspect_image, data)
        ctx.check_cancel()
        ctx.add_units(1, UNIT_CHARS)
        ctx.set_status("translating")
        text = (await ctx.describe_image(data, MIME.get(ctx.ext, "image/png"))).strip()
        ctx.unit_done(1, UNIT_CHARS)
        if not text:
            raise DocumentError(MSG_IMAGE_NO_TEXT)
        ctx.check_cancel()
        ctx.set_status("writing")
        out = ctx.output_with_ext(self.output_ext)
        docx = await asyncio.to_thread(build_docx, text, info, lang_tag(ctx.target_lang))
        await asyncio.to_thread(out.write_bytes, docx)
        return HandlerResult(preview=text[:20_000], pairs=[], output_ext=self.output_ext)
