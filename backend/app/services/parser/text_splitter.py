import re
import jieba
from typing import List, Dict, Any

def segment_chinese(text: str) -> str:
    """Segment text with jieba for FTS5 full-text indexing."""
    if not text:
        return ""
    # Filter out excessive punctuation for clean search tokenization
    words = jieba.cut_for_search(text)
    return " ".join([w.strip() for w in words if w.strip()])

def split_text_into_chunks(text: str, max_chunk_size: int = 600, overlap: int = 80) -> List[str]:
    """
    Split text by natural paragraph boundaries (double newline, then newline, then sentence).
    Ensures chunks do not exceed max_chunk_size while maintaining context.
    """
    if not text or len(text.strip()) == 0:
        return []
        
    paragraphs = re.split(r'\n{2,}', text)
    chunks = []
    current_chunk = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
            
        if current_len + len(para) <= max_chunk_size:
            current_chunk.append(para)
            current_len += len(para)
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
            
            # If paragraph itself is larger than max_chunk_size, split by sentences
            if len(para) > max_chunk_size:
                sentences = re.split(r'([。！？；;\n]+)', para)
                sub_chunk = []
                sub_len = 0
                for i in range(0, len(sentences), 2):
                    sent = sentences[i] + (sentences[i+1] if i+1 < len(sentences) else "")
                    if sub_len + len(sent) <= max_chunk_size:
                        sub_chunk.append(sent)
                        sub_len += len(sent)
                    else:
                        if sub_chunk:
                            chunks.append("".join(sub_chunk))
                        sub_chunk = [sent]
                        sub_len = len(sent)
                if sub_chunk:
                    current_chunk = sub_chunk
                    current_len = sub_len
                else:
                    current_chunk = []
                    current_len = 0
            else:
                current_chunk = [para]
                current_len = len(para)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks
