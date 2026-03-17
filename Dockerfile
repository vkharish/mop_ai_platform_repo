FROM python:3.12-slim

WORKDIR /app

# System deps for PDF parsing (pdfplumber needs libmupdf on some platforms)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create output directories
RUN mkdir -p output/jobs output/uploads static

EXPOSE 8000

# Single worker — pipeline runs are CPU/IO bound in a ThreadPoolExecutor,
# not async-friendly for multiple uvicorn workers
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
