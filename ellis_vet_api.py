#!/usr/bin/env python3
"""
Ellis — Land, Livestock & Sustainable Systems AI — FastAPI Backend v2
Adds on top of v1 (RAG chat): user accounts, pastures, and Pasture Condition
Score history — the foundation for the commercial-grade tool suite (PCS
scorer, paddock planner, etc.) with real per-user persistence.

Storage: SQLite file living inside the SAME persistent volume already
mounted for ChromaDB (host path /data/ellis-knowledge-db -> container path
/app/ellis_knowledge_db) — so accounts.db survives redeploys the exact same
way the knowledge base now does. No new volume needed.

New dependencies vs v1: bcrypt (password hashing), pyjwt (session tokens).
Add both to requirements.txt: bcrypt==4.2.0  pyjwt==2.9.0
"""

import os
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, EmailStr

import bcrypt
import jwt

import chromadb
from chromadb.utils import embedding_functions
import anthropic

app = FastAPI(title="Ellis — Land & Livestock AI", version="2.0.0")

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

# Serves anything in the /static folder at https://ellis.sagewire.dev/tools/<filename>
# e.g. static/pcs-scorer.html -> https://ellis.sagewire.dev/tools/pcs-scorer.html
app.mount("/tools", StaticFiles(directory="static"), name="tools")

# ── CONFIG ──
CHROMA_PATH = "./ellis_knowledge_db"          # same persistent-volume path as v1
SQLITE_PATH = "./ellis_knowledge_db/accounts.db"  # rides on the SAME volume — survives redeploys
KNOWLEDGE_COLLECTION = "ellis_land_livestock_knowledge"
MEMORY_COLLECTION = "ellis_field_memory"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JWT_SECRET = os.environ.get("ELLIS_JWT_SECRET", "")  # MUST be set in Coolify env vars before going live
JWT_ALGO = "HS256"
JWT_EXPIRY_DAYS = 30
PORT = int(os.environ.get("PORT", "5011"))

if not JWT_SECRET:
    print("WARNING: ELLIS_JWT_SECRET is not set. Set it in Coolify environment "
          "variables before accepting real signups — without it, sessions are "
          "insecure. A random 32+ character string is fine.")
    JWT_SECRET = "dev-only-insecure-secret-change-me"

# ── SQLITE SETUP ──
def get_db():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        plan TEXT NOT NULL DEFAULT 'free',
        plan_updated_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pastures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        acres REAL,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS pcs_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pasture_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        scored_at TEXT NOT NULL,
        indicators_json TEXT NOT NULL,
        total_score INTEGER NOT NULL,
        precipitation TEXT,
        temperature_trend TEXT,
        notes TEXT,
        FOREIGN KEY (pasture_id) REFERENCES pastures(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)
    conn.commit()
    conn.close()

init_db()

# ── AUTH HELPERS ──
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False

def create_token(user_id: int, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token")
    return {"user_id": payload["user_id"], "email": payload["email"]}

# ── CHROMA / CLAUDE SETUP (unchanged from v1) ──
ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
chroma = chromadb.PersistentClient(path=CHROMA_PATH)
knowledge_collection = chroma.get_or_create_collection(KNOWLEDGE_COLLECTION, embedding_function=ef)
memory_collection = chroma.get_or_create_collection(MEMORY_COLLECTION, embedding_function=ef)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AuthResponse(BaseModel):
    token: str
    user_id: int
    email: str
    display_name: Optional[str] = None
    plan: str = "free"

class PlanUpdate(BaseModel):
    plan: str  # "free" or "paid" for now — expand later (e.g. "paid_monthly", "paid_annual")

ADMIN_EMAILS = [e.strip() for e in os.environ.get("ELLIS_ADMIN_EMAILS", "").split(",") if e.strip()]
# Set ELLIS_ADMIN_EMAILS in Coolify env vars (comma-separated) to control who can flip plans manually
# until real billing (Stripe or similar) is wired up. Example: "mike@sagewire.dev"

@app.post("/ellis/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (req.email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    password_hash = hash_password(req.password)
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
        (req.email, password_hash, req.display_name, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()

    token = create_token(user_id, req.email)
    return AuthResponse(token=token, user_id=user_id, email=req.email, display_name=req.display_name, plan="free")

@app.post("/ellis/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (req.email,)).fetchone()
    conn.close()

    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = create_token(user["id"], user["email"])
    return AuthResponse(token=token, user_id=user["id"], email=user["email"], display_name=user["display_name"], plan=user["plan"])

@app.get("/ellis/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (current_user["user_id"],)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": user["id"], "email": user["email"], "display_name": user["display_name"], "plan": user["plan"]}

@app.post("/ellis/auth/admin/set-plan/{target_user_id}")
async def set_plan(target_user_id: int, body: PlanUpdate, current_user: dict = Depends(get_current_user)):
    # Manual plan flipping until real billing is wired up. Only emails listed in
    # ELLIS_ADMIN_EMAILS (Coolify env var) can call this.
    if current_user["email"] not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Not authorized")
    if body.plan not in ("free", "paid"):
        raise HTTPException(status_code=400, detail="plan must be 'free' or 'paid'")
    conn = get_db()
    conn.execute(
        "UPDATE users SET plan = ?, plan_updated_at = ? WHERE id = ?",
        (body.plan, datetime.now(timezone.utc).isoformat(), target_user_id)
    )
    conn.commit()
    conn.close()
    return {"user_id": target_user_id, "plan": body.plan}

# ══════════════════════════════════════════════════════════════
# PASTURE ENDPOINTS
# ══════════════════════════════════════════════════════════════

class PastureCreate(BaseModel):
    name: str
    acres: Optional[float] = None
    notes: Optional[str] = None

class PastureOut(BaseModel):
    id: int
    name: str
    acres: Optional[float]
    notes: Optional[str]
    created_at: str

@app.post("/ellis/pastures", response_model=PastureOut)
async def create_pasture(p: PastureCreate, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    created_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO pastures (user_id, name, acres, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (current_user["user_id"], p.name, p.acres, p.notes, created_at)
    )
    conn.commit()
    pasture_id = cursor.lastrowid
    conn.close()
    return PastureOut(id=pasture_id, name=p.name, acres=p.acres, notes=p.notes, created_at=created_at)

@app.get("/ellis/pastures", response_model=List[PastureOut])
async def list_pastures(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pastures WHERE user_id = ? ORDER BY created_at DESC",
        (current_user["user_id"],)
    ).fetchall()
    conn.close()
    return [PastureOut(id=r["id"], name=r["name"], acres=r["acres"], notes=r["notes"], created_at=r["created_at"]) for r in rows]

@app.delete("/ellis/pastures/{pasture_id}")
async def delete_pasture(pasture_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    owned = conn.execute(
        "SELECT id FROM pastures WHERE id = ? AND user_id = ?",
        (pasture_id, current_user["user_id"])
    ).fetchone()
    if not owned:
        conn.close()
        raise HTTPException(status_code=404, detail="Pasture not found")
    conn.execute("DELETE FROM pcs_scores WHERE pasture_id = ?", (pasture_id,))
    conn.execute("DELETE FROM pastures WHERE id = ?", (pasture_id,))
    conn.commit()
    conn.close()
    return {"deleted": True}

# ══════════════════════════════════════════════════════════════
# PASTURE CONDITION SCORE (PCS) ENDPOINTS
# ══════════════════════════════════════════════════════════════

# The 10 official NRCS indicators, each scored 1-5 (see file
# 09_nrcs_pasture_condition_scoring_guide.txt in the knowledge base)
PCS_INDICATORS = [
    "percent_desirable_plants", "percent_legume", "live_plant_cover",
    "plant_diversity", "plant_residue_litter", "grazing_utilization_severity",
    "livestock_concentration_areas", "soil_compaction_regeneration",
    "plant_vigor", "erosion"
]

class PCSSubmit(BaseModel):
    pasture_id: int
    indicators: dict = Field(description=f"Dict with keys: {PCS_INDICATORS}, values 1-5")
    precipitation: Optional[str] = None       # above/normal/below
    temperature_trend: Optional[str] = None   # above/normal/below
    notes: Optional[str] = None

class PCSOut(BaseModel):
    id: int
    pasture_id: int
    scored_at: str
    indicators: dict
    total_score: int
    management_note: str
    precipitation: Optional[str]
    temperature_trend: Optional[str]
    notes: Optional[str]

def pcs_management_note(total: int) -> str:
    if total >= 45: return "No changes needed at this time."
    if total >= 35: return "Minor changes would enhance condition — do the most beneficial first."
    if total >= 25: return "Improvements would benefit productivity and/or environment."
    if total >= 15: return "Needs immediate management changes — high return likely."
    return "Major effort required in time, management, and expense."

@app.post("/ellis/pcs/submit", response_model=PCSOut)
async def submit_pcs(score: PCSSubmit, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    owned = conn.execute(
        "SELECT id FROM pastures WHERE id = ? AND user_id = ?",
        (score.pasture_id, current_user["user_id"])
    ).fetchone()
    if not owned:
        conn.close()
        raise HTTPException(status_code=404, detail="Pasture not found")

    missing = [k for k in PCS_INDICATORS if k not in score.indicators]
    if missing:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Missing indicator scores: {missing}")
    for k, v in score.indicators.items():
        if not isinstance(v, int) or v < 1 or v > 5:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Indicator '{k}' must be an integer 1-5")

    total = sum(score.indicators[k] for k in PCS_INDICATORS)
    scored_at = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        """INSERT INTO pcs_scores
           (pasture_id, user_id, scored_at, indicators_json, total_score, precipitation, temperature_trend, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (score.pasture_id, current_user["user_id"], scored_at, json.dumps(score.indicators),
         total, score.precipitation, score.temperature_trend, score.notes)
    )
    conn.commit()
    score_id = cursor.lastrowid
    conn.close()

    return PCSOut(
        id=score_id, pasture_id=score.pasture_id, scored_at=scored_at,
        indicators=score.indicators, total_score=total,
        management_note=pcs_management_note(total),
        precipitation=score.precipitation, temperature_trend=score.temperature_trend, notes=score.notes
    )

@app.get("/ellis/pcs/history/{pasture_id}", response_model=List[PCSOut])
async def pcs_history(pasture_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    owned = conn.execute(
        "SELECT id FROM pastures WHERE id = ? AND user_id = ?",
        (pasture_id, current_user["user_id"])
    ).fetchone()
    if not owned:
        conn.close()
        raise HTTPException(status_code=404, detail="Pasture not found")

    rows = conn.execute(
        "SELECT * FROM pcs_scores WHERE pasture_id = ? ORDER BY scored_at ASC",
        (pasture_id,)
    ).fetchall()
    conn.close()

    return [
        PCSOut(
            id=r["id"], pasture_id=r["pasture_id"], scored_at=r["scored_at"],
            indicators=json.loads(r["indicators_json"]), total_score=r["total_score"],
            management_note=pcs_management_note(r["total_score"]),
            precipitation=r["precipitation"], temperature_trend=r["temperature_trend"], notes=r["notes"]
        )
        for r in rows
    ]

# ══════════════════════════════════════════════════════════════
# RAG CHAT ENDPOINT (v1, unchanged)
# ══════════════════════════════════════════════════════════════

class EllisQuestion(BaseModel):
    question: str
    location: Optional[str] = None
    acreage: Optional[str] = None
    grazing_method: Optional[str] = None
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

def search_knowledge(question: str, n_results: int = 5):
    try:
        count = knowledge_collection.count()
        if count == 0:
            return []
        results = knowledge_collection.query(query_texts=[question], n_results=min(n_results, count))
        return list(zip(results.get("documents", [[]])[0], results.get("metadatas", [[]])[0]))
    except Exception as e:
        print(f"Knowledge search error: {e}")
        return []

def search_memory(question: str, user_id: str, n_results: int = 3):
    try:
        if memory_collection.count() == 0:
            return []
        results = memory_collection.query(
            query_texts=[question], n_results=min(n_results, memory_collection.count()),
            where={"user_id": {"$eq": user_id}}
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

ELLIS_SYSTEM_PROMPT = """You are Ellis — Expert on Land, Livestock in Sustainable Systems.
Named after a real rancher: three farms run in the 1950s, passed down through the family.

You are a land and livestock management assistant — mob and rotational grazing,
soil health, fencing and water infrastructure, forage/grass species selection,
pasture condition assessment, low-stress stockmanship, and general herd
management (body condition, stockpiling, weed ID).

You are NOT a veterinarian and do not handle animal health, disease, or medical
questions — that's Dave's job (davesvetstation.com). Say so plainly and point
to Dave for those.

Plain language, direct, practical. Show the math on stocking rate / grazing
days / paddock sizing so the person could redo it themselves."""

@app.post("/ellis/ask", response_model=EllisAnswer)
async def ask_ellis(q: EllisQuestion):
    if not q.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    effective_user_id = q.user_id or "default"
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
        question=q.question, answer=answer,
        metadata={"user_id": effective_user_id, "location": q.location or "",
                  "acreage": q.acreage or "", "grazing_method": q.grazing_method or ""}
    )

    return EllisAnswer(
        answer=answer, sources=sources[:3], similar_past_questions=past_summaries[:2],
        confidence="high" if knowledge_results else "low", timestamp=datetime.now().isoformat(),
    )

@app.get("/ellis/status")
async def ellis_status():
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    pasture_count = conn.execute("SELECT COUNT(*) as c FROM pastures").fetchone()["c"]
    score_count = conn.execute("SELECT COUNT(*) as c FROM pcs_scores").fetchone()["c"]
    conn.close()
    return {
        "status": "online",
        "knowledge_docs": knowledge_collection.count(),
        "field_memory_docs": memory_collection.count(),
        "users": user_count,
        "pastures": pasture_count,
        "pcs_scores_logged": score_count,
        "ready": knowledge_collection.count() > 0,
    }

@app.get("/ellis/health")
async def health():
    return {"status": "ok", "service": "Ellis Land & Livestock AI v2"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
