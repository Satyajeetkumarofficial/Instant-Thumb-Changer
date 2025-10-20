FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# For HEIC/HEIF decoding via pillow-heif
RUN apt-get update && apt-get install -y --no-install-recommends libheif1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

EXPOSE 8080
ENV PORT=8080

CMD ["python", "bot.py"]
