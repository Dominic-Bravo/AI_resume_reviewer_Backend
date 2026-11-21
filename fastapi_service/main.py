import asyncio
import io

import pdfplumber
from docx import Document
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI()


@app.get("/health")
def health_check():
    return {"status": "ok"}


class ParseResponse(BaseModel):
    file_name: str
    parsed_text: str
    word_count: int


def _extract_pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(
            page_text
            for page in pdf.pages
            if (page_text := page.extract_text())
        )


def _extract_docx_text(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    return "\n".join(
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    )


@app.post("/api/v1/parse", response_model=ParseResponse)
async def parse_resume_file(
    file: UploadFile = File(...),  # noqa: B008
) -> ParseResponse:
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="A filename is required.",
        )

    filename = file.filename.lower()

    if not filename.endswith((".pdf", ".docx")):
        raise HTTPException(
            status_code=400,
            detail="Only PDF and DOCX files are supported.",
        )

    contents = await file.read()
    extracted_text = ""

    try:
        if filename.endswith(".pdf"):
            extracted_text = await asyncio.to_thread(_extract_pdf_text, contents)
        elif filename.endswith(".docx"):
            extracted_text = await asyncio.to_thread(_extract_docx_text, contents)

    except (ValueError, OSError, Exception) as e:
        raise HTTPException(
            status_code=500,
            detail=f"Text extraction failed: {e!s}",
        ) from e

    extracted_text = extracted_text.strip()

    return ParseResponse(
        file_name=file.filename,
        parsed_text=extracted_text,
        word_count=len(extracted_text.split()),
    )