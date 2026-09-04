FROM python:3.11-slim

WORKDIR /app

# System deps needed for chromadb / sentence-transformers builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install CPU-only torch FIRST, from PyTorch's own CPU wheel index. This
# droplet has no GPU — without this step, sentence-transformers pulls the
# default GPU build of torch, which drags in several GB of NVIDIA CUDA
# packages that aren't needed and can fill the disk during build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

# Copy the app + the already-ingested knowledge base folder
COPY . .

# Same port pattern as Dave/Alice/Joe/Hazel — Ellis gets 5011
EXPOSE 5011

CMD ["python3", "ellis_vet_api.py"]
