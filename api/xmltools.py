"""Small helpers the editor UI needs to turn plain text into PXML-safe text."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class EncodeTextRequest(BaseModel):
    text: str


@router.post("/api/xml/encode-text")
async def encode_text(request: EncodeTextRequest):
    """Entity-encode text exactly as the pipeline does when it writes new content (&sect;, &ldquo;, &hyphen; ...),
    so text a reviewer pastes in matches what the pipeline would have produced."""
    from canada_pipeline.newline_insertions_new import _encode_xml_entities
    return {"xml": _encode_xml_entities(request.text or "")}
