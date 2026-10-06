"""TXT / Markdown / SRT / WebVTT translation that keeps the file structure."""
from __future__ import annotations

import codecs
import re
from functools import lru_cache
from pathlib import Path

from translator_app.documents.base import HandlerContext, Translate, TwoPassHandler, has_letters
from translator_app.documents.inline_tags import translate_unique


# ------------------------------------------------------------- encoding
# Frequent Chinese characters (simplified and traditional forms).  A correct
# GB18030 / Big5 decoding of Chinese text is mostly made of these; a wrong one
# (Korean or Japanese bytes read as Chinese) gives random rare characters.
_ZH_COMMON = frozenset(
    "的一是不了在人有我他这這個个们們中来來上大为為和国國地到以说說时時要就出会會可也你对對生能而子那得于於"
    "着著下自之年过過发發后後作里裡裏用道行所然家种種事成方多经經么麼去法学學如都同现現当當没沒动動面起看"
    "定天分还還进進好小部其些主样樣理心她本前开開但因只从從想实實日军軍者意无無力它与與长長把机機十民第公"
    "此已工使情明性知全三又关關点點正业業外将將两兩高间間由问問很最重并並物手应應战戰向头頭文体體政美相见"
    "見被利什二等产產或新己制身果加西斯月话話合回特代内內信表化老给給世位次度门門任常先海通教儿兒原东東声"
    "聲提立及比员員解水名真论論处處走义義各入几幾口认認条條平系气氣题題活更别別打女变變四神总總何电電数數"
    "安少报報才结結反受目太量再感建务務做接必场場件计計管期市直资資命山金指许許统統区區保至队隊形社便空决"
    "決治展马馬科司五基书書非则則听聽白却界达達光放强強即像难難且权權思王象完设設式色路记記南品住告类類求"
    "据據程北边邊死张張该該交规規万萬取拉格望觉覺术術领領共确確传傳师師观觀清今切院让讓识識候带帶导導争爭"
    "运運"
)
_KANA = re.compile(r"[\u3041-\u30ff]")


@lru_cache(maxsize=1)
def _ks_hangul() -> frozenset:
    """The 2,350 common Hangul syllables of KS X 1001 (what EUC-KR can encode).
    Korean text is made of these; Japanese / Chinese bytes read as CP949 land on
    the rare UHC extension syllables or on Hanja."""
    out = set()
    for cp in range(0xAC00, 0xD7A4):
        try:
            # Python's euc_kr spells the other syllables as 8-byte jamo sequences
            if len(chr(cp).encode("euc_kr")) == 2:
                out.add(chr(cp))
        except UnicodeEncodeError:
            continue
    return frozenset(out)


def _try_decode(data: bytes, enc: str) -> str | None:
    try:
        # incremental: a multi-byte character cut at the end of the sample is fine
        return codecs.getincrementaldecoder(enc)().decode(data, final=False)
    except UnicodeDecodeError:
        return None


def _share(text: str, good) -> float:
    """Share of the non-ASCII letters of ``text`` for which ``good(c)`` holds."""
    letters = [c for c in text if c > "\x7f" and c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if good(c)) / len(letters)


def _western_share(text: str) -> float:
    """Western text (\u00e9, \u00fc, \u00df\u2026) has isolated accented letters inside ASCII words;
    CJK bytes read as CP1252 give long runs of symbols and accented letters."""
    runs = re.findall(r"[^\x00-\x7f]+", text)
    total = sum(len(r) for r in runs)
    if not total:
        return 0.0
    ok = sum(len(r) for r in runs if len(r) <= 2 and all(c.isalpha() or c in _WESTERN_PUNCT for c in r))
    return ok / total


_WESTERN_PUNCT = frozenset("“”‘’‚„–—…•·°±×÷€£¥©®™§¶«»¿¡\u00a0")


def guess_legacy_encoding(data: bytes, sample_size: int = 256 * 1024) -> str:
    """Best guess for non-UTF-8 text: CP949 / Shift-JIS / EUC-JP / CP1252 / GB18030 / Big5."""
    sample = data[:sample_size]
    ko = _try_decode(sample, "cp949")
    if ko is not None and _share(ko, _ks_hangul().__contains__) >= 0.8:
        return "cp949"
    for enc in ("cp932", "euc_jp"):
        ja = _try_decode(sample, enc)
        if ja is not None and _share(ja, lambda c: bool(_KANA.match(c))) >= 0.15:
            return enc
    la = _try_decode(sample, "cp1252")
    if la is not None and _western_share(la) >= 0.8:
        return "cp1252"
    best, best_score = None, 0.15
    for enc in ("gb18030", "cp950"):
        zh = _try_decode(sample, enc)
        if zh is not None and sum(1 for c in zh if c in _ZH_COMMON) >= 3:   # short texts are noisy
            score = _share(zh, _ZH_COMMON.__contains__)
            if score > best_score:
                best, best_score = enc, score
    if best:
        return best
    if ko is not None:
        return "cp949"                                   # short Korean text with rare syllables
    return "cp1252" if la is not None else "latin-1"


def decode_text(data: bytes) -> tuple[str, str]:
    """-> (text, bom) where bom is "\\ufeff" when the output should carry one."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "\ufeff"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "\ufeff"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        pass
    # legacy Windows ANSI file (CP949, GBK, Shift-JIS\u2026): write UTF-8 with a BOM so
    # Notepad/Excel detect it
    return data.decode(guess_legacy_encoding(data), errors="replace"), "\ufeff"


def encode_text(text: str, bom: str) -> bytes:
    return (bom + text).encode("utf-8")


# ------------------------------------------------------------- placeholders
_PH = re.compile(r"<x(\d+)/>")


def protect(s: str, patterns: list[re.Pattern]) -> tuple[str, list[str]]:
    """Replace protected spans (inline code, URLs, tags) by <xN/> placeholders."""
    keep: list[str] = []

    def sub(m):
        keep.append(m.group(0))
        return f"<x{len(keep)}/>"
    # one combined pass: a later pattern must never re-match an earlier placeholder
    rx = re.compile("|".join(f"(?:{p.pattern})" for p in patterns), re.M)
    return rx.sub(sub, s), keep


def restore(s: str, keep: list[str]) -> str:
    used = set()

    def sub(m):
        i = int(m.group(1))
        if 1 <= i <= len(keep):
            used.add(i)
            return keep[i - 1]
        return ""
    s = _PH.sub(sub, s)
    missing = [k for i, k in enumerate(keep, 1) if i not in used]
    return s + ("" if not missing else " " + " ".join(missing))   # never lose code/URLs


def _newline(src: str) -> str:
    return "\r\n" if "\r\n" in src else "\n"


# ------------------------------------------------------------- TXT
def translate_text(src: str, translate: Translate, *, bilingual: bool = False) -> str:
    """Line by line; indentation, blank lines and line endings preserved.
    bilingual: the translated line follows each source line."""
    nl = _newline(src)
    lines = src.splitlines(keepends=True)
    jobs = []
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        if has_letters(body):
            jobs.append((i, body))
    table = translate_unique([b.strip() for _, b in jobs], translate)
    for i, body in jobs:
        lead = body[:len(body) - len(body.lstrip())]
        trail = body[len(body.rstrip()):]
        eol = lines[i][len(body):]
        new = lead + table[body.strip()] + trail
        if bilingual:
            lines[i] = body + (eol or nl) + new + eol
        else:
            lines[i] = new + eol
    return "".join(lines)


# ------------------------------------------------------------- Markdown
MD_PREFIX = re.compile(r"^(\s*(?:>\s*)*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)?)(.*?)(\s*)$")
MD_ATX = re.compile(r"^\s*(?:>\s*)*#{1,6}\s")
MD_ATX_CLOSING = re.compile(r"\s+#+$")                     # "## Title ##" (CommonMark: space before)
FRONT_MATTER_LINE = re.compile(r"^(?:\s|#|-\s|-$|[\w\"'.$@-][^:]*:(?:\s|$))")
FRONT_MATTER_MAX_LINES = 50


def _front_matter_end(lines: list[str]) -> int | None:
    """Index of the closing line of a YAML front matter block that starts at line 0,
    or None when the leading '---' is a thematic break (horizontal rule)."""
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        return None
    for i in range(1, min(len(lines), FRONT_MATTER_MAX_LINES + 1)):
        ln = lines[i].rstrip("\r\n")
        if ln.strip() in ("---", "..."):
            return i
        if ln.strip() and not FRONT_MATTER_LINE.match(ln):
            return None                                   # prose, not "key: value"
    return None
MD_PROTECT = [
    re.compile(r"`+[^`]*`+"),                               # inline code
    re.compile(r"(?<=\])\([^)]*\)"),                       # (url "title") of links/images
    re.compile(r"<https?://[^>]+>|https?://\S+"),           # bare / auto links
    re.compile(r"</?[A-Za-z][^>]*>"),                       # inline HTML
    re.compile(r"\[\^?[^\]]*\]:\s*\S+.*$"),               # reference definitions
]
FENCE = re.compile(r"^\s*(```|~~~)")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def translate_markdown(src: str, translate: Translate, *, bilingual: bool = False) -> str:
    nl = _newline(src)
    lines = src.splitlines(keepends=True)
    in_fence, fence_tok = False, ""
    front_end = _front_matter_end(lines)
    slots = []                  # (line idx, cell idx or None, prefix, protected text, keep, suffix, cells)
    for i, raw in enumerate(lines):
        ln = raw.rstrip("\r\n")
        if front_end is not None and i <= front_end:       # YAML front matter stays as is
            continue
        m = FENCE.match(ln)
        if m:
            if not in_fence:
                in_fence, fence_tok = True, m.group(1)
            elif m.group(1) == fence_tok:
                in_fence = False
            continue
        if in_fence or not has_letters(ln) or TABLE_SEP.match(ln) or ln.lstrip().startswith("<!--"):
            continue
        if "|" in ln and ln.strip().startswith("|"):          # table row: cell by cell
            cells = re.split(r"(?<!\\)\|", ln)
            for c, cell in enumerate(cells):
                if has_letters(cell):
                    p, keep = protect(cell.strip(), MD_PROTECT)
                    lead = cell[:len(cell) - len(cell.lstrip())]
                    trail = cell[len(cell.rstrip()):]
                    slots.append((i, c, lead, p, keep, trail, cells))
            continue
        m = MD_PREFIX.match(ln)
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        if MD_ATX.match(prefix):                           # closing #s of a heading only ("C#" stays)
            c = MD_ATX_CLOSING.search(body)
            if c:
                body, suffix = body[:c.start()], c.group(0) + suffix
        if not has_letters(body):
            continue
        p, keep = protect(body, MD_PROTECT)
        slots.append((i, None, prefix, p, keep, suffix, None))
    table = translate_unique([s[3] for s in slots], translate)
    rows: dict = {}
    new_lines: dict[int, str] = {}
    for i, c, pre, p, keep, suf, cells in slots:
        out = restore(table[p], keep)
        if c is None:
            new_lines[i] = pre + out + suf
        elif bilingual:
            # a second row would break the table (header must precede the separator row)
            rows.setdefault(i, list(cells))[c] = pre + cells[c].strip() + "<br>" + out + suf
        else:
            rows.setdefault(i, list(cells))[c] = pre + out + suf
    for i, cells in rows.items():
        new_lines[i] = "|".join(cells)
    for i, new in new_lines.items():
        body = lines[i].rstrip("\r\n")
        eol = lines[i][len(body):]
        if bilingual:
            if i in rows:
                lines[i] = new + eol                                # source<br>translation per cell
            else:
                # blank line between: the copy becomes its own block (heading, list item, paragraph)
                lines[i] = body + (eol or nl) + (eol or nl) + new + eol
        else:
            lines[i] = new + eol
    return "".join(lines)


# ------------------------------------------------------------- SRT / WebVTT
TIMING = re.compile(r"-->")
SUB_PROTECT = [re.compile(r"</?[A-Za-z][^>]*>"), re.compile(r"\{\\[^}]*\}")]   # <i>, <font>, {\an8}


def translate_subtitles(src: str, translate: Translate) -> str:
    """SRT and WebVTT: index/timing/header/NOTE/STYLE lines untouched; the text lines
    of one cue are translated together (joined by \\n) to keep the sentence."""
    nl = _newline(src)
    # cues are separated by blank lines; a line holding only spaces/tabs counts as blank
    blocks = re.split(r"\r?\n(?:[ \t]*\r?\n)+", src.strip("\ufeff\r\n"))
    jobs = []
    for bi, b in enumerate(blocks):
        ls = b.splitlines()
        if not ls or ls[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        t = next((k for k, line in enumerate(ls) if TIMING.search(line)), None)
        if t is None or t + 1 >= len(ls):
            continue
        text = "\n".join(ls[t + 1:])
        if has_letters(text):
            p, keep = protect(text, SUB_PROTECT)
            jobs.append((bi, t, p, keep))
    table = translate_unique([j[2] for j in jobs], translate)
    for bi, t, p, keep in jobs:
        ls = blocks[bi].splitlines()
        blocks[bi] = nl.join(ls[:t + 1] + restore(table[p], keep).split("\n"))
    bom = "\ufeff" if src.startswith("\ufeff") else ""
    return bom + (nl + nl).join(blocks) + nl


# ------------------------------------------------------------- handler
class PlainTextHandler(TwoPassHandler):
    """.txt .md .markdown .srt .vtt"""

    def __init__(self) -> None:
        self._cache: tuple[str, str] | None = None

    def _source(self, src: Path) -> tuple[str, str]:
        if self._cache is None:
            self._cache = decode_text(src.read_bytes())
        return self._cache

    def _run(self, text: str, translate: Translate, ctx: HandlerContext) -> str:
        bilingual = ctx.output_mode == "bilingual"
        if ctx.ext in (".md", ".markdown"):
            return translate_markdown(text, translate, bilingual=bilingual)
        if ctx.ext in (".srt", ".vtt"):
            return translate_subtitles(text, translate)
        return translate_text(text, translate, bilingual=bilingual)

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        text, bom = self._source(src)
        out = self._run(text, translate, ctx)
        if dst is not None:
            dst.write_bytes(encode_text(out, bom))
            self._output = out

    def preview(self, ctx: HandlerContext, pairs: list[tuple[str, str]]) -> str:
        out = getattr(self, "_output", "")
        return out.lstrip("\ufeff")[:20_000]
