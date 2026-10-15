import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import docx
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.services.parser.text_splitter import split_text_into_chunks

logger = logging.getLogger(__name__)
_CHAPTER = re.compile(r"^第\s*([一二三四五六七八九十百零〇两0-9\s]+)\s*章\s*(.*)$")
_SECTION = re.compile(r"^第\s*[一二三四五六七八九十百零〇两0-9\s]+\s*[节篇]\s*.+$")
_APPENDIX = re.compile(r"^附\s*录(?:\s*[一二三四五六七八九十0-9A-Za-z]+)?(?:\s|$)")
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


class DocxParser:
    @staticmethod
    def _iter_blocks(document: DocumentObject):
        """Read paragraphs, tables, content controls and text boxes in source order."""
        def effective_children(node):
            for child in node.iterchildren():
                if child.tag == _MC + "AlternateContent":
                    # DrawingML and VML often represent the same shape. Select
                    # one representation instead of reading both and duplicating it.
                    alternatives = list(child)
                    selected = next((item for item in alternatives
                                     if any(True for _ in item.iter(qn("w:txbxContent")))), None)
                    if selected is None:
                        selected = next((item for item in alternatives if item.tag == _MC + "Fallback"), None)
                    if selected is not None:
                        yield from effective_children(selected)
                else:
                    yield child

        def text_boxes(node):
            for child in effective_children(node):
                if child.tag == qn("w:txbxContent"):
                    yield child
                else:
                    yield from text_boxes(child)

        def walk(container):
            for child in effective_children(container):
                if isinstance(child, CT_P):
                    yield Paragraph(child, document)
                    # python-docx Paragraph.text does not include text inside shapes.
                    for box in text_boxes(child):
                        yield from walk(box)
                elif isinstance(child, CT_Tbl):
                    yield Table(child, document)
                elif child.tag == qn("w:sdt"):
                    content = child.find(qn("w:sdtContent"))
                    if content is not None:
                        yield from walk(content)
        yield from walk(document.element.body)

    @staticmethod
    def _text(block):
        if isinstance(block, Table):
            return "\n".join(
                " | ".join(cell.text.strip() for cell in row.cells)
                for row in block.rows if any(cell.text.strip() for cell in row.cells)
            ).strip()
        return block.text.strip()

    @staticmethod
    def _chapter_title(text):
        if len(text) > 85:
            return None
        match = _CHAPTER.fullmatch(text)
        if not match or not match.group(2).strip():
            return None
        number = re.sub(r"\s+", "", match.group(1))
        title = re.sub(r"\s+", "", match.group(2))
        # Printed contents with leaders/page numbers are navigation, not headings.
        if re.search(r"[.…·]{2,}|\t|\s\d+\s*$", text):
            return None
        return f"第{number}章 {title}"

    @classmethod
    def parse_docx(
        cls, file_path: Path,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Keep chapter context and real block references; DOCX has no stable pages."""
        document = docx.Document(file_path)
        blocks = list(cls._iter_blocks(document))
        texts = [cls._text(block) for block in blocks]
        total = len(blocks)
        chapter_titles = [cls._chapter_title(text) if not isinstance(block, Table) else None
                          for block, text in zip(blocks, texts)]
        # Many converted books have plain-text contents, with no Word TOC style.
        # A repeated chapter title marks the return from contents to body. Books
        # with learning objectives give a stronger, content-based body anchor.
        contents_start = next((i for i, text in enumerate(texts)
                               if re.sub(r"\s", "", text) in {"目录", "目次", "CONTENTS"}), None)
        body_start = None
        if contents_start is not None:
            seen = set()
            for i in range(contents_start + 1, total):
                title = chapter_titles[i]
                # Contents may include page numbers and dot leaders which a
                # genuine body heading must not include. Keep their identities
                # only for detecting the later start of the body.
                contents_text = re.sub(r"[.…·]{2,}\s*\d*\s*$|\s+\d+\s*$", "", texts[i]).strip()
                contents_title = cls._chapter_title(contents_text)
                if not title:
                    if contents_title:
                        seen.add(contents_title)
                    continue
                next_text = next((s for s in texts[i + 1:i + 8] if s), "")
                style = blocks[i].style.name.lower() if isinstance(blocks[i], Paragraph) and blocks[i].style else ""
                if title in seen or next_text.startswith(("【学习目标】", "学习目标")) or "heading" in style:
                    body_start = i
                    break
                seen.add(title)

        chunks: List[Dict[str, Any]] = []
        chapter = ""
        appendix = ""
        heading = "正文"
        pending: List[str] = []
        start = 1

        def flush(end):
            nonlocal pending
            if pending:
                for content in split_text_into_chunks("\n".join(pending), max_chunk_size=700):
                    chunks.append({"page_number": None, "heading": heading,
                                   "paragraph_ref": f"段落 {start}-{end}", "content": content})
                pending = []

        for i, (block, text) in enumerate(zip(blocks, texts)):
            idx = i + 1
            if not text:
                continue
            in_contents = contents_start is not None and i >= contents_start and (body_start is None or i < body_start)
            new_heading = None
            if in_contents:
                if heading != "目录":
                    new_heading = "目录"
            elif not isinstance(block, Table):
                title = chapter_titles[i]
                style = block.style.name.lower() if block.style else ""
                if _APPENDIX.match(text) and len(text) < 85:
                    appendix = text
                    chapter = ""
                    new_heading = appendix
                elif title:
                    if appendix:
                        new_heading = f"{appendix} / {title}"
                    elif title != chapter:
                        chapter = title
                        new_heading = title
                    # A repeated page header retains the current section context.
                elif len(text) < 60 and (_SECTION.fullmatch(text) or "heading" in style or "title" in style):
                    parent = appendix or chapter
                    new_heading = f"{parent} / {text}" if parent else text
            if new_heading is not None:
                flush(idx - 1)
                heading = new_heading
                start = idx
            if not pending:
                start = idx
            # Headings remain part of source text, so ordinal prose and titles
            # never silently disappear from the retrieval corpus.
            pending.append(text)
            if sum(len(s) + 1 for s in pending) >= 600:
                flush(idx)
            if progress_callback and total:
                progress_callback(int(idx / total * 100), f"正在解析 DOCX 第 {idx}/{total} 个内容块...")
        flush(total)
        return chunks
