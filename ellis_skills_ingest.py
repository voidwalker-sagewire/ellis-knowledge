#!/usr/bin/env python3
"""
Ellis — Skills Ingestion
Loads ellis_skills.json into its own ChromaDB collection, separate from the
knowledge base. Skills = how Ellis should BEHAVE for a kind of question.
Knowledge = what the literature says.

Run from inside the container (or on the server against the same persistent
volume path) after adding/editing skills:

    python3 ellis_skills_ingest.py
    python3 ellis_skills_ingest.py --status
"""

import json
import argparse

import chromadb
from chromadb.utils import embedding_functions

CHROMA_PATH = "./ellis_knowledge_db"   # same persistent volume as everything else
SKILLS_COLLECTION = "ellis_skills"
SKILLS_FILE = "ellis_skills.json"


def get_collection():
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return client.get_or_create_collection(
        name=SKILLS_COLLECTION,
        embedding_function=ef,
        metadata={"description": "Ellis behavioral skills — how to handle a kind of question"}
    )


def skill_to_text(skill: dict) -> str:
    """Flatten a skill into the text that gets embedded and matched against questions."""
    parts = [
        f"SKILL: {skill['name']}",
        f"WHEN TO USE: {skill['when_to_use']}",
        "RULES:",
    ]
    for rule in skill.get("rules", []):
        parts.append(f"- {rule}")
    if skill.get("refusals"):
        parts.append("REFUSALS / HARD LIMITS:")
        for r in skill["refusals"]:
            parts.append(f"- {r}")
    return "\n".join(parts)


def ingest():
    collection = get_collection()

    # Clear existing skills so edits actually take effect (skills are small,
    # so a full replace is simpler and safer than trying to diff them)
    existing = collection.get()
    if existing and existing.get("ids"):
        collection.delete(ids=existing["ids"])
        print(f"Cleared {len(existing['ids'])} existing skill entries")

    with open(SKILLS_FILE, "r", encoding="utf-8") as f:
        skills = json.load(f)

    ids, docs, metas = [], [], []
    for s in skills:
        ids.append(s["id"])
        docs.append(skill_to_text(s))
        tags = s.get("tags", {})
        metas.append({
            "skill_id": s["id"],
            "skill_name": s["name"],
            "topic": tags.get("topic", "general"),
            "voice": tags.get("voice", "principle"),
            "region": tags.get("region", "national"),
        })

    collection.add(ids=ids, documents=docs, metadatas=metas)
    print(f"✅ Ingested {len(ids)} skills into '{SKILLS_COLLECTION}'")
    for s in skills:
        print(f"   - {s['id']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ellis skills ingestion")
    parser.add_argument("--status", action="store_true", help="Show current skill count")
    args = parser.parse_args()

    if args.status:
        c = get_collection()
        print(f"📊 {c.count()} skills loaded in '{SKILLS_COLLECTION}'")
        data = c.get()
        for sid in data.get("ids", []):
            print(f"   - {sid}")
    else:
        ingest()
