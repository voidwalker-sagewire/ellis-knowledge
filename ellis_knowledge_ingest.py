#!/usr/bin/env python3
"""
Ellis — Land, Livestock & Sustainable Systems AI — Document Ingestion Script
Run this on your DigitalOcean server (same box as Dave's Vet Station) to populate
ChromaDB with Ellis's grazing/soil/fencing/forage knowledge.

Uses the SAME ChromaDB instance and embedding model as Dave (no extra memory
footprint for a second model), but a SEPARATE collection so the two knowledge
bases never mix.

Usage:
  python3 ellis_knowledge_ingest.py --txt /path/to/file.txt
  python3 ellis_knowledge_ingest.py --dir /path/to/ellis-knowledge/
  python3 ellis_knowledge_ingest.py --url https://extension.example.edu/...
  python3 ellis_knowledge_ingest.py --status
"""

import os
import sys
import argparse
import hashlib
import requests
from datetime import datetime

# pip install chromadb pypdf2 sentence-transformers beautifulsoup4 requests --break-system-packages
try:
    import chromadb
    from chromadb.utils import embedding_functions
    import PyPDF2
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing dependencies...")
    os.system("pip install chromadb pypdf2 sentence-transformers beautifulsoup4 requests --break-system-packages -q")
    import chromadb
    from chromadb.utils import embedding_functions
    import PyPDF2
    from bs4 import BeautifulSoup

# ── CONFIG ──
CHROMA_HOST = "localhost"
CHROMA_PORT = 8000  # same ChromaDB instance Dave uses
COLLECTION_NAME = "ellis_land_livestock_knowledge"  # SEPARATE collection from herdmate_vet_knowledge
CHUNK_SIZE = 800     # characters per chunk
CHUNK_OVERLAP = 100  # overlap between chunks


def get_chroma_client():
    """Connect to the existing ChromaDB instance (shared with Dave)."""
    try:
        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        client.heartbeat()
        print(f"✅ Connected to ChromaDB at {CHROMA_HOST}:{CHROMA_PORT}")
        return client
    except Exception as e:
        print(f"❌ ChromaDB connection failed: {e}")
        print("Falling back to local persistent storage...")
        return chromadb.PersistentClient(path="./ellis_knowledge_db")


def get_or_create_collection(client):
    """Get or create Ellis's knowledge collection — separate from Dave's."""
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"  # same embedding model as Dave, kept warm by existing cron ping
    )
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"description": "Ellis — land, livestock, grazing, soil & fencing knowledge base"}
    )
    print(f"✅ Collection '{COLLECTION_NAME}' ready — {collection.count()} docs existing")
    return collection


def chunk_text(text, source, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    text = text.strip()
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if len(chunk) > 50:  # skip tiny chunks
            chunk_id = hashlib.md5(f"{source}_{start}".encode()).hexdigest()
            chunks.append({
                "id": chunk_id,
                "text": chunk,
                "metadata": {
                    "source": source,
                    "start_char": start,
                    "ingested_at": datetime.now().isoformat()
                }
            })
        start += chunk_size - overlap
    return chunks


def ingest_txt(collection, txt_path):
    """Read a plain-text file and ingest into ChromaDB."""
    print(f"\n📄 Ingesting text file: {txt_path}")
    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            full_text = f.read()

        chunks = chunk_text(full_text, os.path.basename(txt_path))
        print(f"  Created {len(chunks)} chunks")

        batch_size = 50
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            collection.add(
                ids=[c["id"] for c in batch],
                documents=[c["text"] for c in batch],
                metadatas=[c["metadata"] for c in batch]
            )
            print(f"  Inserted batch {i // batch_size + 1}/{(len(chunks) - 1) // batch_size + 1}")

        print(f"✅ Text file ingested: {len(chunks)} chunks from {os.path.basename(txt_path)}")
        return len(chunks)

    except Exception as e:
        print(f"❌ Text ingestion failed: {e}")
        return 0


def ingest_pdf(collection, pdf_path):
    """Extract text from a PDF and ingest into ChromaDB (kept for future PDF adds)."""
    print(f"\n📄 Ingesting PDF: {pdf_path}")
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            full_text = ""
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    full_text += f"\n[Page {i+1}]\n{text}"
                if i % 10 == 0:
                    print(f"  Reading page {i+1}/{len(reader.pages)}...")

        chunks = chunk_text(full_text, os.path.basename(pdf_path))
        print(f"  Created {len(chunks)} chunks")

        batch_size = 50
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            collection.add(
                ids=[c["id"] for c in batch],
                documents=[c["text"] for c in batch],
                metadatas=[c["metadata"] for c in batch]
            )
            print(f"  Inserted batch {i // batch_size + 1}/{(len(chunks) - 1) // batch_size + 1}")

        print(f"✅ PDF ingested: {len(chunks)} chunks from {os.path.basename(pdf_path)}")
        return len(chunks)

    except Exception as e:
        print(f"❌ PDF ingestion failed: {e}")
        return 0


def ingest_url(collection, url):
    """Scrape a web page (e.g. an extension office article) and ingest into ChromaDB."""
    print(f"\n🌐 Ingesting URL: {url}")
    try:
        headers = {"User-Agent": "Ellis Land & Livestock AI/1.0 (sagewire.dev; educational use)"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        for tag in soup.find_all(['nav', 'footer', 'script', 'style', 'header', 'aside']):
            tag.decompose()

        main = soup.find('main') or soup.find('article') or soup.find('div', class_='content') or soup.body
        text = main.get_text(separator='\n', strip=True) if main else soup.get_text()

        lines = [l.strip() for l in text.split('\n') if l.strip()]
        text = '\n'.join(lines)

        chunks = chunk_text(text, url)
        print(f"  Created {len(chunks)} chunks from {len(text)} chars")

        if chunks:
            collection.add(
                ids=[c["id"] for c in chunks],
                documents=[c["text"] for c in chunks],
                metadatas=[c["metadata"] for c in chunks]
            )

        print(f"✅ URL ingested: {len(chunks)} chunks from {url}")
        return len(chunks)

    except Exception as e:
        print(f"❌ URL ingestion failed: {e}")
        return 0


def ingest_directory(collection, dir_path):
    """Ingest all .txt and .pdf files found in a directory (e.g. the ellis-knowledge repo folder)."""
    total = 0
    txt_files = [f for f in os.listdir(dir_path) if f.lower().endswith('.txt') and f.lower() != 'readme.txt']
    pdf_files = [f for f in os.listdir(dir_path) if f.lower().endswith('.pdf')]

    print(f"\n📁 Found {len(txt_files)} text files and {len(pdf_files)} PDFs in {dir_path}")

    for txt_file in sorted(txt_files):
        full_path = os.path.join(dir_path, txt_file)
        total += ingest_txt(collection, full_path)

    for pdf_file in sorted(pdf_files):
        full_path = os.path.join(dir_path, pdf_file)
        total += ingest_pdf(collection, full_path)

    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ellis — Land, Livestock & Sustainable Systems — Knowledge Ingestion")
    parser.add_argument('--txt', help='Path to a single .txt file')
    parser.add_argument('--pdf', help='Path to a single PDF file')
    parser.add_argument('--url', help='URL to scrape (e.g. an extension office article)')
    parser.add_argument('--dir', help='Directory of .txt/.pdf files (e.g. the cloned ellis-knowledge repo folder)')
    parser.add_argument('--status', action='store_true', help='Show collection status')
    args = parser.parse_args()

    client = get_chroma_client()
    collection = get_or_create_collection(client)

    if args.status:
        print(f"\n📊 Collection status: {collection.count()} documents in Ellis's knowledge base")

    elif args.txt:
        ingest_txt(collection, args.txt)

    elif args.pdf:
        ingest_pdf(collection, args.pdf)

    elif args.url:
        ingest_url(collection, args.url)

    elif args.dir:
        total = ingest_directory(collection, args.dir)
        print(f"\n✅ Directory ingestion complete — {total} total chunks")
        print(f"📊 Total in Ellis's knowledge base: {collection.count()} documents")

    else:
        print("No action specified. Run with --dir /path/to/ellis-knowledge to ingest the starter set,")
        print("or --help for more options.")
