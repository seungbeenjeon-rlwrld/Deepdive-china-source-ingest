"""Turn a filing PDF into readable, right-sized chunks of text.

Why this exists: the pipeline was reaching 巨潮资讯网, finding the IPO
prospectus, and then stopping at the door. Every filing was saved as
``URL_ONLY`` — a title and a PDF link — which for a downstream reader is the
same as not having the document at all. Measured: WebFetch on the Unitree
prospectus returns "raw PDF binary stream ... 382 pages, FlateDecode, JPEG
streams" and gives up.

And the document is where the interesting layer lives. The registered capital,
the legal representative, the affiliate network and the named competitor set
were all sitting in that PDF while a web search for the same facts answered
"go and look at 国家企业信用信息公示系统".

Two jobs, then: decode the PDF, and cut it where a reader would want to open
it. A 382-page filing is not one document to a reader — it is a dozen, and
they only ever want one at a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# A chapter heading sits alone on its own line near the top of the page it
# opens. Cross-references to the same chapter appear mid-sentence and carry
# quotes ("详见第五节 业务与技术"之"四、（二）..."), so requiring a whole-line
# match with no quote characters separates the two. Verified on the Unitree
# prospectus: 12 chapters found, 0 cross-references mistaken for one.
_HEADING = re.compile(r'^\s*(第[一二三四五六七八九十百]+节)\s+([^\n“”"]{2,18})\s*$')

# How far into a page a heading may appear. It is the first thing on the page
# apart from a running header, never buried in body text.
_HEADING_LOOKAHEAD = 6

# Big enough to keep a chapter whole, small enough that a reader can open one
# part without spending its whole context on it. The Unitree prospectus splits
# into 12 chapters of 3k-95k chars; at this cap the largest become 3 parts.
DEFAULT_MAX_SECTION_CHARS = 40_000


@dataclass
class Section:
    """One readable piece of a filing."""

    heading: str
    text: str
    page_start: int          # 1-based, inclusive
    page_end: int            # 1-based, inclusive
    part: int = 1            # 1-based part number within the section
    of_parts: int = 1


@dataclass
class ExtractResult:
    sections: list[Section] = field(default_factory=list)
    page_count: int = 0
    char_count: int = 0
    # Why nothing came back, when nothing came back. A filing that yields no
    # text must say so rather than vanish, or it looks like a filing with no
    # content instead of one we failed to read.
    error: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)


def extract_sections(
    data: bytes,
    *,
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
    max_pages: int = 1200,
) -> ExtractResult:
    """Read a PDF's text and cut it into sections a reader can open.

    Returns an :class:`ExtractResult` rather than raising, because one
    unreadable filing among twenty must not sink the other nineteen.
    """
    try:
        import pypdf
    except ImportError:  # pragma: no cover - dependency is declared
        return ExtractResult(error="pypdf is not installed")

    try:
        reader = pypdf.PdfReader(_BytesIO(data))
        pages = reader.pages
    except Exception as exc:
        return ExtractResult(error=f"could not open the PDF: {exc}")

    page_count = len(pages)
    if page_count > max_pages:
        return ExtractResult(
            page_count=page_count,
            error=f"{page_count} pages exceeds the {max_pages}-page limit",
        )

    texts: list[str] = []
    failed_pages = 0
    for page in pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            # A single malformed page is normal in scanned filings. Keep its
            # slot so page numbers stay honest.
            texts.append("")
            failed_pages += 1

    total = sum(len(t) for t in texts)
    if total < 200:
        # A filing scanned as images has a text layer of nothing. Saying so is
        # useful; pretending we read it is not.
        return ExtractResult(
            page_count=page_count,
            char_count=total,
            error="no text layer (the filing is probably scanned images)",
            notes={"failed_pages": failed_pages},
        )

    sections = _split(texts, max_section_chars=max_section_chars)
    return ExtractResult(
        sections=sections,
        page_count=page_count,
        char_count=total,
        notes={"failed_pages": failed_pages, "sections": len(sections)},
    )


def _split(texts: list[str], *, max_section_chars: int) -> list[Section]:
    starts = _heading_pages(texts)
    if not starts:
        # No chapter structure — a short announcement, or a layout the pattern
        # does not know. Chunk by size so the output shape stays the same.
        return _chunk(
            Section(heading="全文", text="".join(texts), page_start=1,
                    page_end=len(texts)),
            max_section_chars=max_section_chars,
        )

    sections: list[Section] = []
    # Anything before the first heading is the cover and table of contents.
    # It is worth keeping — the cover carries the issuer, the underwriter and
    # the filing date — but it is not part of chapter one.
    if starts[0][0] > 0:
        front = "".join(texts[: starts[0][0]])
        if len(front.strip()) >= 200:
            sections.append(Section(heading="封面与目录", text=front,
                                    page_start=1, page_end=starts[0][0]))

    bounds = [index for index, _ in starts] + [len(texts)]
    for position, (index, heading) in enumerate(starts):
        end = bounds[position + 1]
        sections.append(Section(
            heading=heading,
            text="".join(texts[index:end]),
            page_start=index + 1,
            page_end=end,
        ))

    out: list[Section] = []
    for section in sections:
        out.extend(_chunk(section, max_section_chars=max_section_chars))
    return out


def _heading_pages(texts: list[str]) -> list[tuple[int, str]]:
    """Pages that open a chapter, as (0-based page index, heading)."""
    found: list[tuple[int, str]] = []
    for index, text in enumerate(texts):
        for line in text.split("\n")[:_HEADING_LOOKAHEAD]:
            match = _HEADING.match(line)
            if match:
                found.append((index, f"{match.group(1)} {match.group(2).strip()}"))
                break
    return found


def _chunk(section: Section, *, max_section_chars: int) -> list[Section]:
    """Cut an oversized section on paragraph boundaries."""
    if len(section.text) <= max_section_chars:
        return [section]

    paragraphs = section.text.split("\n")
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if current and size + len(paragraph) > max_section_chars:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 1
    if current:
        parts.append("\n".join(current))

    return [
        Section(
            heading=section.heading,
            text=text,
            page_start=section.page_start,
            page_end=section.page_end,
            part=number,
            of_parts=len(parts),
        )
        for number, text in enumerate(parts, start=1)
    ]


def _BytesIO(data: bytes):
    import io

    return io.BytesIO(data)
