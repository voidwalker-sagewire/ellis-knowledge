# Ellis — Operations README

Expert on Land, Livestock in Sustainable Systems. This is the "how do I start,
stop, deploy, and troubleshoot this thing" doc — not the knowledge base index
(that's in the older README.txt in this same repo).

---

## What's actually running

- **Server:** `dave-brain-server` (DigitalOcean droplet, 159.203.129.179)
- **Deployment manager:** Coolify (same box as Dave/Alice/Joe/Hazel)
- **Container port:** 5011 (internal only — not published to the host, same
  as the other four vet assistants. Traefik routes to it, the host itself
  can't `curl localhost:5011` directly.)
- **GitHub repo:** `voidwalker-sagewire/ellis-knowledge` — ONE repo, holds
  everything: knowledge `.txt` files, the ingest script, the API
  (`ellis_vet_api.py`), the HTML tools, `Dockerfile`, `requirements.txt`.

## Storage — what survives a redeploy and what doesn't

- **Knowledge base (ChromaDB)** and **accounts/pastures/scores (SQLite)**
  both live inside `./ellis_knowledge_db/` in the container.
- That folder is mounted to a **Coolify persistent volume**: host path
  `/data/ellis-knowledge-db` → container path `/app/ellis_knowledge_db`.
- Because of that volume, both the knowledge base AND all user
  accounts/pastures/PCS history survive a redeploy. You do NOT need to
  re-run the ingest script after every deploy — only when you add NEW
  knowledge files.
- If you ever see the doc count or user count reset to zero after a
  deploy, the first thing to check is whether that volume mount is still
  configured in Coolify (Persistent Storage tab on the resource).

## Environment variables required

Set these in Coolify → Ellis resource → Environment Variables:

| Variable | Purpose | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Calls Claude for answers | Yes — use a **standard key scoped to Default Workspace**, not an identity-linked key (identity-linked keys need an extra `anthropic-workspace-id` header this app doesn't send) |
| `ELLIS_JWT_SECRET` | Signs login session tokens | Yes before any real signups — any long random string. Without it, the app runs but falls back to an insecure default and prints a warning in the logs. |
| `ELLIS_ADMIN_EMAILS` | Comma-separated list of emails allowed to flip a user's plan (free/paid) | Optional — only needed once you want to manually mark someone as paid |
| `PORT` | Which port the app listens on | Optional, defaults to 5011 |

## Everyday commands

**Check what's running:**
```
docker ps | grep -i ellis
```
Watch the "Up" time. If it keeps resetting to "Up X seconds," it's
crash-looping — go straight to logs.

**Read the logs (swap in the container ID from `docker ps`):**
```
docker logs <container_id> --tail 50
```

**Hit an endpoint from inside the server** (port isn't published to host,
so plain `curl localhost:5011/...` from the server itself won't work):
```
docker exec -it <container_id> python3 -c "
import urllib.request, json
req = urllib.request.Request('http://localhost:5011/ellis/status')
print(urllib.request.urlopen(req).read().decode())
"
```
Note: this container image doesn't have `curl` installed — use Python's
`urllib` like above for any manual endpoint checks from inside it.

**Re-run ingest after adding NEW knowledge files** (only needed when you
add files, not on every deploy):
```
cd ~/ellis-knowledge
git pull
python3 ellis_knowledge_ingest.py --dir .
```

## Deploying a change

1. Edit/add files in the GitHub repo (`voidwalker-sagewire/ellis-knowledge`)
2. In Coolify → Ellis resource → hit **Redeploy**
3. Watch the deployment log until it says "Rolling update completed"
4. Check `docker ps | grep -i ellis` — confirm it's staying up, not
   restarting
5. Check logs for a clean startup (no tracebacks)

## API endpoints (v2 — accounts + PCS tracking)

**Chat (RAG, from v1):**
- `POST /ellis/ask` — ask Ellis a question, gets an answer grounded in the
  knowledge base
- `GET /ellis/status` — doc count, user count, pasture count, PCS score
  count, ready flag
- `GET /ellis/health` — basic liveness check

**Accounts:**
- `POST /ellis/auth/register` — email, password (8+ chars), optional
  display_name → returns a token
- `POST /ellis/auth/login` — email, password → returns a token
- `GET /ellis/auth/me` — requires `Authorization: Bearer <token>` header,
  returns the logged-in user's info including plan
- `POST /ellis/auth/admin/set-plan/{user_id}` — admin-only (email must be
  in `ELLIS_ADMIN_EMAILS`), manually flips a user between "free" and
  "paid" until real billing exists

**Pastures:**
- `POST /ellis/pastures` — create (name, optional acres/notes)
- `GET /ellis/pastures` — list the logged-in user's pastures
- `DELETE /ellis/pastures/{id}` — delete a pasture and its score history

**Pasture Condition Score:**
- `POST /ellis/pcs/submit` — pasture_id + all 10 indicator scores (1-5
  each) + optional precipitation/notes → saves and returns the total +
  management-tier note
- `GET /ellis/pcs/history/{pasture_id}` — full score history for a pasture,
  oldest first

All account-scoped endpoints require the `Authorization: Bearer <token>`
header from login/register.

## Known open items (not yet built)

- **Billing/payments** — the `plan` field exists on user accounts but
  nothing charges anyone yet. Manual flipping only, via the admin
  endpoint above.
- **`ellis.sagewire.dev` subdomain** — DNS + Traefik routing not yet set
  up. The HTML tools (`pcs-scorer.html` etc.) point at
  `https://ellis.sagewire.dev` as their API base — that won't resolve
  until this is done.
- **Other planned tools** (not yet built): paddock size planner, stockpile
  grazing planner, water system sizer, BCS-to-feeding-target converter,
  weed/toxic plant ID flow, fence/corral spec helper, seasonal reminder
  generator.
- **Microservices** — flagged as a maybe-future need, nothing decided or
  built yet. If/when other services need to trust Ellis logins, they can
  validate the same JWT as long as they're given the same
  `ELLIS_JWT_SECRET` — no separate auth system needed.

## Troubleshooting things that have already bitten us once

- **`anthropic` + `httpx` version mismatch** → `TypeError: Client.__init__()
  got an unexpected keyword argument 'proxies'`. Fixed by pinning
  `anthropic==0.39.0` and `httpx==0.27.2` in `requirements.txt`. Don't
  loosen these without testing.
- **ChromaDB "connection failed, falling back to local storage"** — this is
  expected. There is no shared ChromaDB service on this server; each
  assistant (Dave/Alice/Joe/Hazel/Ellis) runs its own embedded ChromaDB.
  Don't try to point Ellis at a shared instance that doesn't exist.
- **Copying a pre-built ChromaDB folder between environments** can cause a
  `KeyError: '_type'` crash from version mismatches. Always run the ingest
  script fresh inside the actual container/environment that will serve it,
  rather than copying a `.sqlite3` file built somewhere else.
