FROM python:3.11-slim

# System dependencies required for OCR (Tesseract + Spanish language pack)
# and so PyMuPDF can render pages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

# PROD: uploads/ and prototype.db live on the container filesystem.
# Containers are ephemeral, so for real production you would:
#   - move prototype.db -> Postgres (external connection, not local disk)
#   - move uploads/ -> S3/R2 (not local disk)
# For the prototype, if your hosting provider offers a persistent volume,
# mount it at /app/backend so data survives redeploys.

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
