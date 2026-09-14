import pytest
from app.services.parser.text_splitter import split_text_into_chunks, segment_chinese

def test_split_text_into_chunks():
    sample_text = "第一段文字，关于管理学基本原理。\n\n第二段文字，关于组织设计的基本原则与部门划分机制。\n\n第三段文字，控制系统与反馈闭环。"
    chunks = split_text_into_chunks(sample_text, max_chunk_size=25)
    assert len(chunks) >= 3
    assert "管理学基本原理" in chunks[0]
    assert "第三段文字" in chunks[-1]

def test_segment_chinese():
    text = "矩阵制组织结构的特征与适用条件"
    segmented = segment_chinese(text)
    assert "组织结构" in segmented or "矩阵" in segmented
    assert len(segmented.split()) >= 2
