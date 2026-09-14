import docx
import logging
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional
from docx.document import Document as DocumentObject
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from app.services.parser.text_splitter import split_text_into_chunks

logger = logging.getLogger(__name__)

class DocxParser:
    @staticmethod
    def _iter_blocks(document: DocumentObject):
        """Yield paragraphs and tables in their original document order."""
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, document)
            elif isinstance(child, CT_Tbl):
                yield Table(child, document)

    @classmethod
    def parse_docx(
        cls, 
        file_path: Path, 
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Parses a DOCX document using paragraph numbers and heading hierarchy.
        Explicitly avoids faking page numbers.
        """
        doc = docx.Document(file_path)
        blocks = list(cls._iter_blocks(doc))
        total_paras = len(blocks)
        
        chunks: List[Dict[str, Any]] = []
        current_heading = "正文"
        current_paras = []
        start_para_idx = 1
        
        for idx, block in enumerate(blocks, start=1):
            if isinstance(block, Table):
                text = "\n".join(
                    " | ".join(cell.text.strip() for cell in row.cells)
                    for row in block.rows
                    if any(cell.text.strip() for cell in row.cells)
                ).strip()
                style_name = "table"
            else:
                text = block.text.strip()
                style_name = block.style.name.lower() if block.style else ""
            if not text:
                continue

            is_heading = "heading" in style_name or "title" in style_name or text.startswith(("第", "一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、"))

            if is_heading and len(text) < 60:
                # Flush previous accumulated text
                if current_paras:
                    full_block = "\n".join(current_paras)
                    sub_chunks = split_text_into_chunks(full_block, max_chunk_size=700)
                    for sub in sub_chunks:
                        chunks.append({
                            "page_number": None,
                            "heading": current_heading,
                            "paragraph_ref": f"段落 {start_para_idx}-{idx-1}",
                            "content": sub
                        })
                    current_paras = []
                current_heading = text
                start_para_idx = idx
            else:
                current_paras.append(text)
                if len("\n".join(current_paras)) >= 600:
                    full_block = "\n".join(current_paras)
                    sub_chunks = split_text_into_chunks(full_block, max_chunk_size=700)
                    for sub in sub_chunks:
                        chunks.append({
                            "page_number": None,
                            "heading": current_heading,
                            "paragraph_ref": f"段落 {start_para_idx}-{idx}",
                            "content": sub
                        })
                    current_paras = []
                    start_para_idx = idx + 1

            if progress_callback and total_paras > 0:
                pct = int((idx / total_paras) * 100)
                progress_callback(pct, f"正在解析 DOCX 第 {idx}/{total_paras} 段落...")

        if current_paras:
            full_block = "\n".join(current_paras)
            sub_chunks = split_text_into_chunks(full_block, max_chunk_size=700)
            for sub in sub_chunks:
                chunks.append({
                    "page_number": None,
                    "heading": current_heading,
                    "paragraph_ref": f"段落 {start_para_idx}-{total_paras}",
                    "content": sub
                })

        return chunks
