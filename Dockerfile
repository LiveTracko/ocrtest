# Render.com deploy — Python 3.12, system libs for OpenCV.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OpenCV / Pillow runtime libs (PaddleOCR itself is lazy-loaded at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libsm6 libxext6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Render provides $PORT; default 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
