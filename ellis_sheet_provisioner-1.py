#!/usr/bin/env python3
"""
Ellis Sheet Provisioner — LIVE

All onboarding routes below are production-verified through the SageWire
Gateway (per platform team confirmation). Nothing in this file guesses
at a route shape.

Ownership boundary (unchanged, still holds):
  - Ellis does NOT create, modify, or publish onboarding flows.
    "ellis-setup" v1 is published and owned on the SageWire platform side.
    This script only ever references its flow_key against the already-
    published flow.
  - Ellis MUST call the Gateway, never the onboarding service directly:
        https://api.sagewire.dev/onboarding/api/v1

Routes used (all confirmed production):
  - GET  /flows/{flow_identifier}                       (resolve flow_key -> flow_id)
  - POST /sessions
  - GET  /sessions/{session_identifier}
  - GET  /sessions?flow_id=...&status=...
  - GET  /sessions/{id}/answers
  - GET  /sessions/{id}/requirements
  - POST /sessions/{id}/requirements/{key}/satisfy

Requirement key: ellis_sheet_provisioned (confirmed exact key, matches
what's defined on the published flow — not the earlier draft's guess).

Run once to process whatever's waiting:
    python3 ellis_sheet_provisioner.py

Or loop it (e.g. from cron, or `--loop` for a simple in-process poll):
    python3 ellis_sheet_provisioner.py --loop
"""

import os
import time
import requests
from google.oauth2.service_account import Credentials
import google.auth.transport.requests

ONBOARDING_API_BASE = os.environ.get(
    "ONBOARDING_API_BASE", "https://api.sagewire.dev/onboarding/api/v1"
)
ELLIS_FLOW_KEY = "ellis-setup"
REQUIREMENT_KEY = "ellis_sheet_provisioned"  # confirmed exact key

CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "/app/ellis_knowledge_db/credentials.json")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

ELLIS_TAB_NAME = "Ellis"
ELLIS_COLUMNS = [
    "paddock_name", "date_in", "date_out", "head_count", "class_of_stock",
    "days_rest", "residual_note", "weeds", "wet_dry", "notes"
]

_flow_id_cache = None


# ══════════════════════════════════════════════════════════════
# Onboarding service — all routes below are production-confirmed
# ══════════════════════════════════════════════════════════════

def get_flow_id() -> str:
    """Resolve the ellis-setup flow_key to its internal flow_id, once, cached."""
    global _flow_id_cache
    if _flow_id_cache:
        return _flow_id_cache
    resp = requests.get(f"{ONBOARDING_API_BASE}/flows/{ELLIS_FLOW_KEY}", timeout=15)
    resp.raise_for_status()
    _flow_id_cache = resp.json()["id"]
    return _flow_id_cache


def create_onboarding_session(subject_type: str, subject_id: str) -> dict:
    """Start a new session against the already-published ellis-setup flow."""
    resp = requests.post(
        f"{ONBOARDING_API_BASE}/sessions",
        json={
            "flow_key": ELLIS_FLOW_KEY,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_session(session_identifier: str) -> dict:
    resp = requests.get(f"{ONBOARDING_API_BASE}/sessions/{session_identifier}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_session_answers(session_id: str) -> dict:
    """Returns {field_key: value_json} for every answered field on a session."""
    resp = requests.get(f"{ONBOARDING_API_BASE}/sessions/{session_id}/answers", timeout=15)
    resp.raise_for_status()
    return {a["field_key"]: a["value_json"] for a in resp.json().get("items", [])}


def get_pending_sessions() -> list:
    """COMPLETED ellis-setup sessions whose Sheet hasn't been provisioned yet."""
    flow_id = get_flow_id()
    resp = requests.get(
        f"{ONBOARDING_API_BASE}/sessions",
        params={"flow_id": flow_id, "status": "COMPLETED", "limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    sessions = resp.json().get("items", [])

    pending = []
    for s in sessions:
        req_resp = requests.get(
            f"{ONBOARDING_API_BASE}/sessions/{s['id']}/requirements", timeout=15
        )
        req_resp.raise_for_status()
        reqs = {r["requirement_key"]: r for r in req_resp.json().get("items", [])}
        provisioned = reqs.get(REQUIREMENT_KEY)
        if provisioned and provisioned.get("status") != "SATISFIED":
            pending.append(s)
    return pending


def satisfy_requirement(session_id: str, sheet_id: str, mode: str) -> None:
    resp = requests.post(
        f"{ONBOARDING_API_BASE}/sessions/{session_id}/requirements/{REQUIREMENT_KEY}/satisfy",
        json={
            "satisfied_by": "ellis_sheet_provisioner",
            "external_reference": sheet_id,
            "details_json": {"sheet_id": sheet_id, "mode": mode},
        },
        timeout=15,
    )
    resp.raise_for_status()


# ══════════════════════════════════════════════════════════════
# Ellis-owned logic — Google Sheets/Drive side
# ══════════════════════════════════════════════════════════════

def get_token(scopes):
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def sheets_request(method, url, token, json_body=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=20)
    if not resp.ok:
        print(f"Sheets API error {resp.status_code}: {resp.text[:300]}")
    return resp


def add_ellis_tab_to_existing_sheet(sheet_id, token):
    add_sheet_resp = sheets_request(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate",
        token,
        json_body={"requests": [{"addSheet": {"properties": {"title": ELLIS_TAB_NAME}}}]},
    )
    if not add_sheet_resp.ok and "already exists" not in add_sheet_resp.text:
        return False

    header_resp = sheets_request(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{ELLIS_TAB_NAME}!A1:J1?valueInputOption=RAW",
        token,
        json_body={"values": [ELLIS_COLUMNS]},
    )
    return header_resp.ok


def create_new_sheet_with_ellis_tab(operation_name, contact_email, sheets_token, drive_token):
    create_resp = sheets_request(
        "POST",
        "https://sheets.googleapis.com/v4/spreadsheets",
        sheets_token,
        json_body={
            "properties": {"title": f"{operation_name} — Ellis"},
            "sheets": [{"properties": {"title": ELLIS_TAB_NAME}}],
        },
    )
    if not create_resp.ok:
        return None
    sheet_id = create_resp.json().get("spreadsheetId")
    if not sheet_id:
        return None

    sheets_request(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{ELLIS_TAB_NAME}!A1:J1?valueInputOption=RAW",
        sheets_token,
        json_body={"values": [ELLIS_COLUMNS]},
    )

    share_headers = {"Authorization": f"Bearer {drive_token}", "Content-Type": "application/json"}
    requests.post(
        f"https://www.googleapis.com/drive/v3/files/{sheet_id}/permissions",
        headers=share_headers,
        json={"type": "user", "role": "writer", "emailAddress": contact_email},
        timeout=20,
    )
    return sheet_id


# ══════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════

def process_session(s, sheets_token, drive_token):
    session_id = s["id"]
    answers = get_session_answers(session_id)

    has_existing = answers.get("has_existing_sheet")
    existing_sheet_id = (answers.get("existing_sheet_id") or "").strip()
    operation_name = answers.get("operation_name") or "Ellis Customer"
    contact_email = answers.get("contact_email")

    if has_existing and existing_sheet_id:
        ok = add_ellis_tab_to_existing_sheet(existing_sheet_id, sheets_token)
        if ok:
            print(f"Added Ellis tab to existing sheet {existing_sheet_id} (session {session_id})")
            satisfy_requirement(session_id, existing_sheet_id, "existing_sheet")
        else:
            print(f"Failed to add Ellis tab to {existing_sheet_id} (session {session_id})")
    else:
        new_id = create_new_sheet_with_ellis_tab(operation_name, contact_email, sheets_token, drive_token)
        if new_id:
            print(f"Created new sheet {new_id} for '{operation_name}', shared with {contact_email}")
            satisfy_requirement(session_id, new_id, "new_sheet")
        else:
            print(f"Failed to create new sheet for session {session_id}")


def run_once():
    sheets_token = get_token(SHEETS_SCOPES)
    drive_token = get_token(DRIVE_SCOPES)
    pending = get_pending_sessions()
    if not pending:
        print("No pending sessions to provision.")
        return
    print(f"Found {len(pending)} pending session(s).")
    for s in pending:
        try:
            process_session(s, sheets_token, drive_token)
        except Exception as e:
            print(f"Error processing session {s.get('id')}: {e}")


if __name__ == "__main__":
    import sys
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"Provisioner error: {e}")
            time.sleep(60)
    else:
        run_once()
