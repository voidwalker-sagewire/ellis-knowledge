FROM python:3.11-slim

WORKDIR /app

# System deps needed for chromadb / sentence-transformers builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app + the already-ingested knowledge base folder
COPY . .

# Same port pattern as Dave/Alice/Joe/Hazel — Ellis gets 5011
EXPOSE 5011

CMD ["python3", "ellis_vet_api.py"]
