#!/usr/bin/env python3
"""
Ellis — Land, Livestock & Sustainable Systems AI — FastAPI Backend v1
Same pattern as HerdMate DAVE / Alice / Joe / Hazel. Standalone container,
own port, own ChromaDB collection persisted to local disk (no shared Chroma
service exists on this box — confirmed via the ingest script's fallback).
"""

import os
import hashlib
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import chromadb
from chromadb.utils import embedding_functions
import anthropic

app = FastAPI(title="Ellis — Land & Livestock AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ellis.sagewire.dev",
        "https://davesvetstation.com",
        "http://localhost:3000",
        "http://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CONFIG ──
# No shared ChromaDB service exists on this server (confirmed: ingest script's
# HttpClient connection to localhost:8000 fails and falls back to
# PersistentClient). Match that same path here so this app reads the SAME data
# the ingest script already wrote to disk.
CHROMA_PATH = "./ellis_knowledge_db"
KNOWLEDGE_COLLECTION = "ellis_land_livestock_knowledge"
MEMORY_COLLECTION = "ellis_field_memory"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PORT = int(os.environ.get("PORT", "5011"))  # next free port after Herdmate(5005)/Alice(5006)/Hazel(5007)/Joe(5008)

# ── CHROMA CLIENT ──
def get_chroma():
    return chromadb.PersistentClient(path=CHROMA_PATH)

ef = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="all-MiniLM-L6-v2"  # same model Dave/Alice/Joe/Hazel use — keeps it warm via the existing cron ping
)

chroma = get_chroma()
knowledge_collection = chroma.get_or_create_collection(KNOWLEDGE_COLLECTION, embedding_function=ef)
memory_collection = chroma.get_or_create_collection(MEMORY_COLLECTION, embedding_function=ef)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── MODELS ──
class EllisQuestion(BaseModel):
    question: str
    location: Optional[str] = None       # e.g. "Sublette County, WY" — free text, no GPS required
    acreage: Optional[str] = None
    grazing_method: Optional[str] = None  # continuous / rotational / mob
    user_id: Optional[str] = "default"
    conversation_history: list = Field(default_factory=list)
    image_base64: Optional[str] = None
    image_type: Optional[str] = "image/jpeg"

class EllisAnswer(BaseModel):
    answer: str
    sources: list
    similar_past_questions: list
    confidence: str
    timestamp: str

# ── RAG ──
def search_knowledge(question: str, n_results: int = 5):
    try:
        count = knowledge_collection.count()
        if count == 0:
            return []
        results = knowledge_collection.query(
            query_texts=[question],
            n_results=min(n_results, count)
        )
        return list(zip(results.get("documents", [[]])[0], results.get("metadatas", [[]])[0]))
    except Exception as e:
        print(f"Knowledge search error: {e}")
        return []

def search_memory(question: str, user_id: str, n_results: int = 3):
    try:
        if memory_collection.count() == 0:
            return []
        where_filter = {"user_id": {"$eq": user_id}}
        results = memory_collection.query(
            query_texts=[question],
            n_results=min(n_results, memory_collection.count()),
            where=where_filter
        )
        return list(zip(results.get("documents", [[]])[0], results.get("metadatas", [[]])[0]))
    except Exception as e:
        print(f"Memory search error: {e}")
        return []

def save_to_memory(question: str, answer: str, metadata: dict):
    try:
        doc_id = hashlib.md5(f"{question}_{datetime.now().isoformat()}".encode()).hexdigest()
        memory_collection.add(
            ids=[doc_id],
            documents=[f"Q: {question}\nA: {answer}"],
            metadatas=[{**metadata, "timestamp": datetime.now().isoformat(), "type": "field_question"}]
        )
    except Exception as e:
        print(f"Memory save error: {e}")

# ── SYSTEM PROMPT ──
ELLIS_SYSTEM_PROMPT = """You are Ellis — Expert on Land, Livestock in Sustainable Systems.
Named after a real rancher: three farms run in the 1950s, passed down through the family.

You are a land and livestock management assistant for working ranchers — mob and
rotational grazing, soil health, fencing and water infrastructure, forage/grass
species selection, pasture condition assessment, low-stress stockmanship, and
general herd management (body condition, stockpiling, weed ID).

You are NOT a veterinarian and do not handle animal health, disease, or medical
questions — that's Dave's job (the vet AI at davesvetstation.com). If a question is
clearly a health/medical question, say so plainly and point to Dave.

Your style:
- Plain language. Direct. Get to the point fast.
- Practical — what to actually DO, not just theory.
- Confident where the knowledge base backs it up; honest about uncertainty
  where it doesn't.
- A little dry humor is fine. Respect the person's time and intelligence —
  no lecturing, no sermons about regenerative ag philosophy unless asked.

When math is involved (stocking rate, grazing days, paddock sizing, water
system sizing), show the actual numbers and the formula, not just a
conclusion — the rancher should be able to redo the math themselves next time.

You have access to:
1. A knowledge base of USDA/NRCS and university extension material on grazing,
   soil, fencing, water systems, forage species, low-stress handling, and
   livestock nutrition/safety
2. The rancher's own past questions and your past answers, for continuity"""

# ── MAIN ENDPOINT ──
@app.post("/ellis/ask", response_model=EllisAnswer)
async def ask_ellis(q: EllisQuestion):
    if not q.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    effective_user_id = q.user_id or "default"

    # RAG search
    knowledge_results = search_knowledge(q.question)
    past_questions = search_memory(q.question, effective_user_id)

    dynamic_system = ELLIS_SYSTEM_PROMPT

    ctx_parts = []
    if q.location: ctx_parts.append(f"Location: {q.location}")
    if q.acreage: ctx_parts.append(f"Acreage: {q.acreage}")
    if q.grazing_method: ctx_parts.append(f"Grazing method: {q.grazing_method}")
    if ctx_parts:
        dynamic_system += "\n\n--- FIELD CONTEXT ---\n" + "\n".join(ctx_parts)

    sources = []
    if knowledge_results:
        knowledge_ctx = "\n\n--- LAND & LIVESTOCK KNOWLEDGE BASE ---"
        for doc, meta in knowledge_results:
            source = meta.get("source", "extension reference")
            knowledge_ctx += f"\n[{source}]\n{doc}\n"
            if source not in sources:
                sources.append(source)
        dynamic_system += knowledge_ctx

    past_summaries = []
    if past_questions:
        mem_ctx = "\n\n--- YOUR PAST QUESTIONS ---"
        for doc, meta in past_questions:
            ts = meta.get("timestamp", "")[:10]
            mem_ctx += f"\n[{ts}] {doc}\n"
            past_summaries.append(f"{ts}: {doc[:100]}...")
        dynamic_system += mem_ctx

    claude_messages = []
    for msg in q.conversation_history[-8:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            claude_messages.append({"role": role, "content": content})

    if q.image_base64:
        current_content = [
            {"type": "image", "source": {"type": "base64", "media_type": q.image_type or "image/jpeg", "data": q.image_base64}},
            {"type": "text", "text": q.question}
        ]
    else:
        current_content = q.question

    claude_messages.append({"role": "user", "content": current_content})

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=dynamic_system,
            messages=claude_messages
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        answer = text_blocks[0].text if text_blocks else "Ellis could not generate a response."
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI response failed: {str(e)}")

    save_to_memory(
        question=q.question,
        answer=answer,
        metadata={
            "user_id": effective_user_id,
            "location": q.location or "",
            "acreage": q.acreage or "",
            "grazing_method": q.grazing_method or "",
        }
    )

    return EllisAnswer(
        answer=answer,
        sources=sources[:3],
        similar_past_questions=past_summaries[:2],
        confidence="high" if knowledge_results else "low",
        timestamp=datetime.now().isoformat(),
    )

@app.get("/ellis/status")
async def ellis_status():
    return {
        "status": "online",
        "knowledge_docs": knowledge_collection.count(),
        "field_memory_docs": memory_collection.count(),
        "ready": knowledge_collection.count() > 0,
    }

@app.get("/ellis/health")
async def health():
    return {"status": "ok", "service": "Ellis Land & Livestock AI v1"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
