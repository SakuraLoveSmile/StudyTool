"""Wire models for the connected, source-grounded knowledge library."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LibraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def unique_document_ids(cls, value, info):
        if info.field_name.endswith("doc_ids"):
            if any(not item.strip() for item in value):
                raise ValueError("资料 ID 不能为空")
            return list(dict.fromkeys(item.strip() for item in value))
        return value


class GuideRequest(LibraryRequest):
    doc_id: str = Field(min_length=1, max_length=300)
    chapter_id: str = Field(min_length=1, max_length=300)
    management_doc_ids: list[str] = Field(default_factory=list, max_length=50)
    exam_doc_ids: list[str] = Field(default_factory=list, max_length=50)


class AskRequest(LibraryRequest):
    question: str = Field(min_length=1, max_length=4000)
    doc_ids: list[str] = Field(min_length=1, max_length=50)
    chapter_id: str | None = Field(default=None, max_length=300)
    previous_answer_id: str | None = Field(default=None, max_length=300)


class Chapter(BaseModel):
    id: str
    doc_id: str
    title: str
    chunk_count: int
    character_count: int


class LibraryContent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class Citation(LibraryContent):
    chunk_id: str
    doc_id: str
    doc_name: str
    location: str
    quote: str = Field(min_length=1)


class Concept(LibraryContent):
    name: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    recitation: str = Field(min_length=1)
    keywords: list[str]
    citations: list[Citation] = Field(min_length=1)


class MindMapLeaf(LibraryContent):
    title: str = Field(min_length=1)


class MindMapBranch(MindMapLeaf):
    children: list[MindMapLeaf] = Field(default_factory=list)


class MindMap(MindMapLeaf):
    children: list[MindMapBranch] = Field(min_length=1)


class Connection(LibraryContent):
    accounting_concept: str = Field(min_length=1)
    management_concept: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    application: str = Field(min_length=1)
    evidence_level: Literal["suggestion", "textbook_supported"]
    citations: list[Citation] = Field(default_factory=list)


class ExamPoint(BaseModel):
    concept: str
    question_id: str
    year: int | None
    stem: str
    doc_name: str
    location: str


class Guide(LibraryContent):
    id: str
    doc_id: str
    chapter_id: str
    chapter_title: str
    summary: str = Field(min_length=1)
    concepts: list[Concept] = Field(min_length=1)
    mind_map: MindMap
    connections: list[Connection] = Field(default_factory=list)
    exam_points: list[ExamPoint] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    created_at: str
    source_stale: bool = False


class Answer(BaseModel):
    id: str
    question: str
    answer: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    doc_ids: list[str]
    chapter_id: str | None = None
    previous_answer_id: str | None = None
    created_at: str
    source_stale: bool = False
