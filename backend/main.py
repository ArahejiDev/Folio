"""
Folio / EasyStudy - PDF -> swipeable True/False study cards, generated with AI.

Prototype architecture (designed so it can move to production without a rewrite):

  Client (frontend/index.html)
        |
        v
  FastAPI (this file)  --------->  SQLite (here) / Postgres (production)
        |                                   ^
        | enqueues job                      |
        v                                   |
  BackgroundTasks (here) / Celery + Redis (production)
        |
        v
  Groq API (OpenAI-compatible) -> generates JSON questions -> stored in the DB

Things that would change in production (marked with "# PROD:" in the code):
  - SQLite -> Postgres (the schema is already shaped for it)
  - BackgroundTasks -> a Celery worker separate from the web process
  - PDF storage on local disk -> S3 / Cloudflare R2
  - Single process -> web servers and workers scaled independently
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import io

import fitz  # PyMuPDF: text extraction + page rendering to image
import httpx
import pytesseract
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

OCR_LANG = "spa+eng"  # Tesseract language data packs to use
OCR_MIN_CHARS = 20    # pages yielding fewer chars than this are assumed to be scanned

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "prototype.db"
UPLOAD_DIR = BASE_DIR / "uploads"  # PROD: S3/R2 bucket instead of local disk
UPLOAD_DIR.mkdir(exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"  # good quality/cost ratio on Groq for this task; has a free tier
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

N_QUESTIONS = 30  # number of cards to generate per document (prototype)

app = FastAPI(title="Folio - PDF to study cards (prototype)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # PROD: restrict to the real frontend domain
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Database (SQLite for the prototype -> Postgres in production)
# --------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                content_hash TEXT UNIQUE NOT NULL,  -- cache key per document
                filename TEXT NOT NULL,
                title TEXT,                  -- display title shown on the card (metadata or heuristic)
                status TEXT NOT NULL DEFAULT 'processing',  -- processing | ready | failed
                error TEXT,
                progress_done INTEGER NOT NULL DEFAULT 0,   -- cards generated so far
                progress_total INTEGER NOT NULL DEFAULT 0,  -- target number of cards (N_QUESTIONS)
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id),
                statement TEXT NOT NULL,     -- statement to be judged true or false
                is_true INTEGER NOT NULL,    -- 1 = true, 0 = false
                explanation TEXT,            -- snippet/justification from the source text
                order_index INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS answers (
                id TEXT PRIMARY KEY,
                question_id TEXT NOT NULL REFERENCES questions(id),
                is_correct INTEGER NOT NULL,
                answered_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Soft migration for databases created before these columns existed
        for ddl in (
            "ALTER TABLE documents ADD COLUMN title TEXT",
            "ALTER TABLE documents ADD COLUMN progress_done INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE documents ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists


init_db()


# --------------------------------------------------------------------------
# PDF text extraction
# --------------------------------------------------------------------------

def ocr_page(page: "fitz.Page") -> str:
    """Render a PDF page as an image and run OCR on it.
    Only used for pages where normal extraction yielded no text
    (typically a scanned PDF, or a page that is just an image/photo)."""
    # dpi=200 is a good quality/speed balance for OCR of regular text.
    # PROD: for pages with very small print, raise to 300 dpi at the cost of speed.
    pix = page.get_pixmap(dpi=200)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img, lang=OCR_LANG)


def extract_text(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    parts = []
    pages_ocr = 0

    for page in doc:
        text = (page.get_text() or "").strip()

        if len(text) < OCR_MIN_CHARS:
            # Page has almost no text -> probably a scan, fall back to OCR
            ocr_text = ocr_page(page).strip()
            if ocr_text:
                text = ocr_text
                pages_ocr += 1

        if text:
            parts.append(text)

    doc.close()
    full_text = "\n".join(parts)

    if not full_text.strip():
        raise ValueError(
            "No se pudo extraer texto del PDF ni con OCR (puede ser un escaneo de muy baja calidad)."  # user-facing (Spanish UI)
        )

    if pages_ocr:
        print(f"[OCR] {pages_ocr} page(s) of {pdf_path.name} processed with OCR")

    # Simple context limit for the prototype: truncate to ~40k characters
    # (split into several chunks to generate cards in batches, see generate_questions)
    # PROD: proper chunking + per-chunk generation.
    return full_text[:40000]


def extract_title(pdf_path: Path, full_text: str, fallback_filename: str) -> str:
    """Try to get a nice display title for the card, in this order:
    1) PDF metadata (if the author set it when exporting the document)
    2) the first line of text that looks like a title (neither too short nor too long)
    3) the file name, without the .pdf extension
    """
    try:
        doc = fitz.open(str(pdf_path))
        meta_title = (doc.metadata or {}).get("title", "").strip()
        doc.close()
        if meta_title and 3 <= len(meta_title) <= 120:
            return meta_title
    except Exception:
        pass

    for line in full_text.splitlines():
        candidate = line.strip()
        if 8 <= len(candidate) <= 90:
            return candidate

    return Path(fallback_filename).stem


# --------------------------------------------------------------------------
# Groq API call to generate the questions
# --------------------------------------------------------------------------

QUESTION_GEN_PROMPT = """Eres un generador de material de estudio tipo "verdadero o falso". A partir del siguiente texto extraido de un documento, genera EXACTAMENTE {n} afirmaciones sobre los conceptos mas importantes del texto. Ni una menos: {n} es un requisito estricto, no una sugerencia. Aproximadamente la mitad deben ser verdaderas y la mitad falsas (mezcladas, sin patron fijo).

Reglas de idioma (MUY IMPORTANTE):
- Escribe TODO ("statement" y "explanation") en español natural y fluido de España/Latinoamerica, como lo escribiria una persona hispanohablante.
- NO mezcles palabras o expresiones en ingles, NO calques literalmente estructuras gramaticales del ingles, y NO dejes terminos sin traducir salvo nombres propios o siglas que ya se usan tal cual en español.
- Si el texto original esta en otro idioma, traduce el contenido de forma natural al español (no palabra por palabra); el resultado debe leerse como si se hubiera escrito directamente en español.

Reglas de contenido:
- Cada afirmacion debe poder verificarse UNICAMENTE con informacion del texto proporcionado.
- Las afirmaciones falsas deben ser sutiles y plausibles (p.ej. cambiar una cifra, una fecha, invertir una causa/efecto, atribuir algo a la entidad equivocada), nunca absurdas o evidentemente falsas.
- Para CADA afirmacion, incluye en "explanation" UNA sola frase corta (maximo 20 palabras) extraida o parafraseada del texto original que demuestre por que es verdadera o por que es falsa. Se breve: es preferible una frase corta y clara a una larga.
- No inventes informacion que no este en el texto.
- No repitas el mismo concepto en varias afirmaciones; cubre la mayor variedad posible de conceptos del texto (fechas, definiciones, causas/efectos, cifras, nombres, etc).
- No repitas la misma afirmacion, ni de forma parcial, en dos tarjetas distintas.
- Ve directo al JSON: no expliques tu razonamiento, no pienses en voz alta, no escribas nada antes o despues del JSON. Genera las {n} afirmaciones directamente.

Devuelve UNICAMENTE un JSON valido (sin texto adicional, sin markdown, sin ```), con esta forma exacta y EXACTAMENTE {n} elementos en la lista:

{{
  "questions": [
    {{
      "statement": "afirmacion a evaluar como verdadera o falsa, en español",
      "is_true": true,
      "explanation": "frase corta, en español, que confirma o desmiente la afirmacion"
    }}
  ]
}}

Texto del documento:
---
{text}
---
"""


BATCH_SIZE = 5  # cards requested per AI call; small batches are more reliable than asking for many at once


def _split_into_chunks(text: str, n_chunks: int) -> list[str]:
    """Split the text into n_chunks pieces of similar size, cutting at newlines
    near the ideal cut point so sentences are not split in half when possible."""
    if n_chunks <= 1:
        return [text]
    target_len = max(len(text) // n_chunks, 500)
    chunks = []
    start = 0
    for i in range(n_chunks):
        if i == n_chunks - 1:
            chunks.append(text[start:])
            break
        end = start + target_len
        # look for the next newline near the ideal cut point
        break_at = text.find("\n", end)
        if break_at == -1 or break_at - end > 400:
            break_at = end
        chunks.append(text[start:break_at])
        start = break_at
    return [c.strip() for c in chunks if c.strip()]


def _parse_retry_wait(error_body: str, attempt: int) -> float:
    """Groq states in the error message itself how long to wait
    (e.g. 'Please try again in 13.0125s'). Use that value plus a small
    margin; if absent, fall back to exponential backoff."""
    match = re.search(r"try again in ([\d.]+)s", error_body)
    if match:
        return float(match.group(1)) + 1.0  # 1s safety margin
    return float(2 ** (attempt + 2))  # fallback: 4s, 8s, 16s, 32s...


async def _call_groq(client: httpx.AsyncClient, text_chunk: str, n: int, retries: int = 5) -> list[dict]:
    prompt = QUESTION_GEN_PROMPT.format(n=n, text=text_chunk)

    for attempt in range(retries + 1):
        resp = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                # openai/gpt-oss-120b is a "reasoning" model: before writing the final
                # answer it spends tokens on hidden internal reasoning, and that
                # also counts against max_tokens. With "low" we ask it to think as
                # little as possible so it has plenty of tokens left to write the
                # full JSON (otherwise it sometimes runs out of tokens and returns
                # an empty response -> "json_validate_failed" error).
                "reasoning_effort": "low",
                "max_tokens": 2000,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Respondes siempre en español natural y fluido. "
                            "Nunca mezclas palabras o frases en ingles, ni traduces literalmente "
                            "estructuras del ingles al español."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                # Force a valid JSON response (supported by Groq models via the OpenAI-compatible API)
                "response_format": {"type": "json_object"},
            },
        )

        if resp.status_code == 400 and attempt < retries:
            # Sometimes the model runs out of tokens after reasoning and returns an
            # empty generation (json_validate_failed). It is intermittent: retry.
            print(f"[groq] 400 (empty generation), retrying (attempt {attempt + 1}/{retries})")
            await asyncio.sleep(2.0)
            continue

        if resp.status_code == 429 and attempt < retries:
            wait = _parse_retry_wait(resp.text, attempt)
            print(f"[groq] 429 rate limit, retrying in {wait:.1f}s (attempt {attempt + 1}/{retries})")
            await asyncio.sleep(wait)
            continue

        if resp.status_code != 200:
            raise RuntimeError(f"Error de la API de Groq: {resp.status_code} {resp.text}")

        break

    data = resp.json()
    raw_text = data["choices"][0]["message"]["content"]

    # Defensive cleanup in case the model adds markdown fences despite response_format
    cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"La IA no devolvio JSON valido: {e}\nRespuesta cruda: {raw_text[:500]}")

    return parsed.get("questions", [])


async def generate_questions(text: str, document_id: str) -> list[dict]:
    if not GROQ_API_KEY:
        raise RuntimeError(
            "Falta GROQ_API_KEY en el entorno. Exporta tu clave antes de arrancar el backend."  # user-facing (Spanish UI)
        )

    # Asking for many cards (e.g. 30) in ONE call is unreliable: the model tends
    # to ignore the requested number and generate noticeably fewer. Instead, we
    # split the text into several chunks and request a small batch (BATCH_SIZE)
    # per chunk, one after another.
    #
    # Since each batch may also return fewer cards than requested (or some
    # invalid ones we discard), we do NOT settle for a single pass: we keep
    # requesting extra batches -recycling text chunks if needed- until
    # N_QUESTIONS is reached, with a call cap as a safety net so we never loop
    # forever if something is really wrong.
    n_batches = max(1, -(-N_QUESTIONS // BATCH_SIZE))  # ceiling division
    chunks = _split_into_chunks(text, n_batches)
    MAX_CALLS = max(20, n_batches * 4)

    with get_db() as conn:
        conn.execute(
            "UPDATE documents SET progress_done = 0, progress_total = ? WHERE id = ?",
            (N_QUESTIONS, document_id),
        )

    all_questions: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    call_count = 0

    async with httpx.AsyncClient(timeout=90.0) as client:
        # SEQUENTIAL calls (not concurrent): firing them all at once against the
        # Groq free tier triggers its rate limit and several batches fail silently.
        while len(all_questions) < N_QUESTIONS and call_count < MAX_CALLS:
            chunk = chunks[call_count % len(chunks)]
            try:
                batch = await _call_groq(client, chunk, BATCH_SIZE)
                added = 0
                skipped = 0
                for q in batch:
                    # Defensive validation: sometimes the model returns an element that
                    # is not the expected object (e.g. a bare string), or is missing
                    # fields. Discard it instead of crashing the job.
                    if not isinstance(q, dict) or "statement" not in q or "is_true" not in q:
                        skipped += 1
                        continue
                    key = str(q["statement"]).strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        all_questions.append(q)
                        added += 1
                print(
                    f"[groq] call {call_count + 1}/{MAX_CALLS}: requested {BATCH_SIZE}, "
                    f"new useful {added} (discarded {skipped}), "
                    f"total {len(all_questions)}/{N_QUESTIONS}"
                )
                # Progress visible to the frontend (progress bar on the study screen)
                with get_db() as conn:
                    conn.execute(
                        "UPDATE documents SET progress_done = ? WHERE id = ?",
                        (min(len(all_questions), N_QUESTIONS), document_id),
                    )
            except Exception as e:
                print(f"[groq] call {call_count + 1}/{MAX_CALLS} failed: {e}")
                errors.append(str(e))

            call_count += 1
            if len(all_questions) < N_QUESTIONS and call_count < MAX_CALLS:
                await asyncio.sleep(6.0)  # gap between calls to stay under the free tier TPM limit

    if not all_questions:
        raise RuntimeError(
            "La IA no genero ninguna pregunta. Errores: " + "; ".join(errors[:3])
        )

    if len(all_questions) < N_QUESTIONS:
        print(
            f"[groq] warning: only got {len(all_questions)}/{N_QUESTIONS} "
            f"after {call_count} calls (safety limit reached)"
        )

    return all_questions[:N_QUESTIONS]


# --------------------------------------------------------------------------
# Processing pipeline (in production this would be a Celery job)
# --------------------------------------------------------------------------

async def process_document(document_id: str, pdf_path: Path):
    try:
        text = extract_text(pdf_path)
        title = extract_title(pdf_path, text, pdf_path.name)
        questions = await generate_questions(text, document_id)

        with get_db() as conn:
            for i, q in enumerate(questions):
                conn.execute(
                    """INSERT INTO questions
                       (id, document_id, statement, is_true, explanation, order_index)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        document_id,
                        q["statement"],
                        int(bool(q["is_true"])),
                        q.get("explanation", ""),
                        i,
                    ),
                )
            conn.execute(
                "UPDATE documents SET status = 'ready', title = ? WHERE id = ?",
                (title, document_id),
            )
    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', error = ? WHERE id = ?",
                (str(e), document_id),
            )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.post("/api/documents")
async def upload_document(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se admiten archivos PDF.")

    content = await file.read()
    content_hash = hashlib.sha256(content).hexdigest()  # cache key per document

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, status FROM documents WHERE content_hash = ?", (content_hash,)
        ).fetchone()

    if existing:
        # Document seen before -> zero AI cost, serve what was already generated
        return {"document_id": existing["id"], "status": existing["status"], "cached": True}

    document_id = str(uuid.uuid4())
    pdf_path = UPLOAD_DIR / f"{document_id}.pdf"
    pdf_path.write_bytes(content)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO documents (id, content_hash, filename, title, status) VALUES (?, ?, ?, ?, 'processing')",
            (document_id, content_hash, file.filename, Path(file.filename).stem),
        )

    # PROD: enqueue a Celery job here instead of BackgroundTasks
    background_tasks.add_task(process_document, document_id, pdf_path)

    return {"document_id": document_id, "status": "processing", "cached": False}


@app.get("/api/documents")
async def list_documents():
    # PROD: filter by authenticated user here (WHERE user_id = ?)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT d.id, d.filename, COALESCE(NULLIF(d.title, ''), d.filename) AS title,
                      d.status, d.created_at, d.progress_done, d.progress_total,
                      COUNT(q.id) AS question_count
               FROM documents d
               LEFT JOIN questions q ON q.document_id = d.id
               GROUP BY d.id
               ORDER BY d.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/documents/{document_id}")
async def get_document_status(document_id: str):
    with get_db() as conn:
        doc = conn.execute(
            """SELECT id, status, error, filename, progress_done, progress_total
               FROM documents WHERE id = ?""",
            (document_id,),
        ).fetchone()
    if not doc:
        raise HTTPException(404, "Documento no encontrado.")
    return dict(doc)


@app.get("/api/documents/{document_id}/questions")
async def get_questions(document_id: str):
    with get_db() as conn:
        doc = conn.execute("SELECT status FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not doc:
            raise HTTPException(404, "Documento no encontrado.")
        if doc["status"] != "ready":
            raise HTTPException(409, f"El documento todavia no esta listo (status: {doc['status']}).")

        rows = conn.execute(
            """SELECT id, statement, is_true, explanation
               FROM questions WHERE document_id = ? ORDER BY order_index""",
            (document_id,),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "statement": r["statement"],
            "is_true": bool(r["is_true"]),
            "explanation": r["explanation"],
        }
        for r in rows
    ]


@app.post("/api/questions/{question_id}/answer")
async def submit_answer(question_id: str, payload: dict):
    is_correct = bool(payload.get("is_correct"))
    with get_db() as conn:
        q = conn.execute("SELECT id FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not q:
            raise HTTPException(404, "Pregunta no encontrada.")
        conn.execute(
            "INSERT INTO answers (id, question_id, is_correct) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), question_id, int(is_correct)),
        )
    return {"ok": True}


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

FRONTEND_DIR = BASE_DIR.parent / "frontend"


@app.get("/")
async def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")