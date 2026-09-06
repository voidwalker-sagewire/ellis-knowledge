# Ellis — Operations README

Expert on Land, Livestock in Sustainable Systems. This is the "how do I start,
stop, deploy, and troubleshoot this thing" doc — not the knowledge base index
(that's in the older README.txt in this same repo).

---

## What's actually running — CONFIRMED LIVE

- **Server:** `[SERVER_HOSTNAME - see internal ops wiki, not here]` (DigitalOcean droplet, [SERVER_IP - see internal ops wiki, not here])
- **Deployment manager:** Coolify (same box as Dave/Alice/Joe/Hazel)
- **Domain:** `https://ellis.sagewire.dev` — **confirmed live**, DNS and
  Traefik routing both work end to end (was an open item, is now done)
- **Container port:** 5011 (internal only — not published to the host, same
  as the other four vet assistants. Traefik routes to it, the host itself
  can't `curl localhost:5011` directly.)
- **GitHub repo:** `voidwalker-sagewire/ellis-knowledge` — ONE repo, holds
  everything: knowledge `.txt` files, skills (`ellis_skills.json`), the
  ingest scripts, the API (`ellis_vet_api.py`), the Sheets provisioner
  (`ellis_sheet_provisioner.py`), the HTML tools under `static/`,
  `Dockerfile`, `requirements.txt`.

## Storage — what survives a redeploy and what doesn't

- **Knowledge base (ChromaDB), skills (ChromaDB), accounts/pastures/scores
  (SQLite), and the Google credentials file** all live inside
  `./ellis_knowledge_db/` in the container.
- That folder is mounted to a **Coolify persistent volume**: host path
  `/data/ellis-knowledge-db` → container path `/app/ellis_knowledge_db`.
  Everything above survives a redeploy because of this one mount.
- You do NOT need to re-run the knowledge ingest after every deploy — only
  when you add NEW knowledge files. Same logic for skills — only re-run
  `ellis_skills_ingest.py` when a skill is added or edited (it wipes and
  replaces on every run, so it's safe to re-run any time).
- If the doc count, skill count, or user count resets to zero after a
  deploy, check whether the volume mount is still configured in Coolify
  (Persistent Storage tab on the resource) before anything else.

## Environment variables required

| Variable | Purpose | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Calls Claude for answers | Yes — standard key scoped to Default Workspace, not an identity-linked key |
| `ELLIS_JWT_SECRET` | Signs login session tokens | Yes before real signups — long random string |
| `ELLIS_ADMIN_EMAILS` | Comma-separated emails allowed to flip a user's plan (free/paid) | Optional |
| `GOOGLE_CREDENTIALS_FILE` | Path to the Google service account JSON | Yes, for Sheets provisioning. Set to `/app/ellis_knowledge_db/credentials.json` — **not** `/root/credentials.json` (that was a wrong guess early on; the real file lives on the persistent volume) |
| `ONBOARDING_API_BASE` | SageWire onboarding gateway base URL | Defaults to `https://api.sagewire.dev/onboarding/api/v1` — never point this at `onboarding.sagewire.dev` directly, only the gateway |
| `PORT` | Which port the app listens on | Optional, defaults to 5011 |

**Getting the credentials file onto Ellis's volume** (one-time, already done
once but documented in case it's ever needed again):
```
docker cp <dave_container_id>:/data/credentials.json /tmp/credentials.json
docker cp /tmp/credentials.json <ellis_container_id>:/app/ellis_knowledge_db/credentials.json
```
This is DCC Dave's service account, currently shared — email:
`herdmate-dave-bot@herdmate-uhf-tag-scanner.iam.gserviceaccount.com`

## Everyday commands

**Check what's running:**
```
docker ps | grep -i ellis
```
Container ID changes on every redeploy — always re-check before running
anything with `docker exec`. Watch the "Up" time; if it keeps resetting to
"Up X seconds," it's crash-looping — go straight to logs.

**Read the logs:**
```
docker logs <container_id> --tail 50
```

**Hit an endpoint from inside the server** (port isn't published to host):
```
docker exec -it <container_id> python3 -c "
import urllib.request
req = urllib.request.Request('http://localhost:5011/ellis/status')
print(urllib.request.urlopen(req).read().decode())
"
```
Or, since the domain is live, just curl it directly from the server —
this actually works now:
```
curl -s https://ellis.sagewire.dev/ellis/status
```

**Re-run knowledge ingest** (only after adding NEW knowledge files):
```
cd ~/ellis-knowledge && git pull
python3 ellis_knowledge_ingest.py --dir .
```

**Re-run skills ingest** (after adding/editing a skill in `ellis_skills.json`):
```
docker exec -it <container_id> python3 ellis_skills_ingest.py
```

**Run the Sheets provisioner** (finds BLOCKED onboarding sessions waiting on
Ellis, builds/extends their Google Sheet, reports completion):
```
docker exec -it <container_id> python3 ellis_sheet_provisioner.py
```

## Deploying a change

1. Edit/add files in the repo
2. Coolify → Ellis resource → **Redeploy**
3. Watch the log until "Rolling update completed"
4. `docker ps | grep -i ellis` — confirm it's staying up
5. Check logs for a clean startup
6. **Verify the change actually took** — `docker exec -it <id> grep -A2
   "<function name>" /app/<file>.py` to confirm the deployed code matches
   what you meant to upload. This has bitten us before (uploaded a fix,
   redeployed, container still ran old code — turned out the upload itself
   hadn't taken).

## API endpoints

**Chat (RAG + skills):**
- `POST /ellis/ask` — question in, answer out. Response includes
  `skills_used` (which behavioral skills fired) and `sources` (which
  knowledge docs got cited). Accepts optional `location`, `acreage`,
  `grazing_method`, `image_base64` (for plant ID photos), and
  `conversation_history`.
- `GET /ellis/status` — knowledge doc count, skills loaded, user/pasture/
  PCS counts, ready flag
- `GET /ellis/health` — basic liveness check

**Accounts:**
- `POST /ellis/auth/register`, `POST /ellis/auth/login` — email/password,
  8+ chars, returns a token
- `GET /ellis/auth/me` — requires `Authorization: Bearer <token>`
- `POST /ellis/auth/admin/set-plan/{user_id}` — admin-only (email in
  `ELLIS_ADMIN_EMAILS`), manually flips free/paid until real billing exists

**Pastures / PCS (SQLite) — STATUS: BEING REPLACED, see note below:**
- `POST /ellis/pastures`, `GET /ellis/pastures`, `DELETE /ellis/pastures/{id}`
- `POST /ellis/pcs/submit`, `GET /ellis/pcs/history/{pasture_id}`

> **Architecture decision, made after these endpoints were built:** paddock
> and PCS data is moving to **Google Sheets** as the source of truth
> (matching Dave's existing Sheets pattern) instead of living in Ellis's own
> SQLite tables. The endpoints above still exist and still work, but they
> are not the long-term design — don't build new features on top of them.
> The PCS scorer frontend (`pcs-scorer.html`) still points at these SQLite
> endpoints and needs rework once the Sheets side is further along.

## Frontend tools (served as static files)

All served from the same container under `/tools/`:
- `https://ellis.sagewire.dev/tools/chat.html` — the actual conversational
  interface, cloned from Dave's working pattern. Shows sources and
  `skills_used` under each answer. **This is the main way to actually talk
  to Ellis** — everything else is a specialized form.
- `https://ellis.sagewire.dev/tools/grazing-calculator.html` — grazing days
  math (acreage, yield, method → grazing days). Stateless, nothing saved.
- `https://ellis.sagewire.dev/tools/grazing-plan-checklist.html` — fillable
  intake form, generates a written plan doc. Stateless, nothing saved.
- `https://ellis.sagewire.dev/tools/grazing-chart.html` — visual rotation
  timeline. Stateless, nothing saved, browser-tab-only data.
- `https://ellis.sagewire.dev/tools/pcs-score` — pasture condition scorer,
  the one tool with real login + persistence (SQLite, see note above about
  this moving to Sheets).

**Known gap:** these four tools + the pastures/PCS storage are not unified
— no shared login across the calculator/checklist/chart, no cross-linking
between them (chat.html links out to the others; they don't link back or to
each other). Flagged, not yet fixed.

## Google Sheets / Onboarding integration

**Ownership boundary (confirmed by SageWire platform team) — do not violate:**
- Ellis does NOT create, modify, or publish onboarding flows. The
  `ellis-setup` flow is owned and published on the SageWire platform side.
  Ellis only ever references its `flow_key` against the already-published
  version.
- Ellis MUST call the gateway (`https://api.sagewire.dev/onboarding/api/v1`),
  never the onboarding service directly.

**The flow (`ellis-setup`, v1, PUBLISHED, subject_type `ORGANIZATION`):**
- Step `operation`: `operation_name` (text), `contact_email` (text)
- Step `sheet_setup`: `has_existing_sheet` (boolean), `existing_sheet_id`
  (text, only visible/required if `has_existing_sheet` is true)
- One requirement: `ellis_sheet_provisioned` (EXTERNAL, satisfied by
  `ellis_sheet_provisioner.py`, not by the onboarding service itself)

**What `ellis_sheet_provisioner.py` does:**
1. Resolves the `ellis-setup` flow_key to its internal flow_id
2. Finds sessions with `status: BLOCKED` (not `COMPLETED` — sessions with a
   pending required EXTERNAL requirement sit at BLOCKED until something
   satisfies it; this tripped us up once already, see troubleshooting below)
   and requirement `ellis_sheet_provisioned` not yet `SATISFIED`
3. Reads the session's answers
4. If `has_existing_sheet` is true: adds an "Ellis" tab (with header row)
   to the sheet at `existing_sheet_id` — **confirmed working**
5. If false: attempts to create a brand-new spreadsheet — **currently fails
   with a 403**, see known limitation below
6. Reports back via `POST /sessions/{id}/requirements/ellis_sheet_provisioned/satisfy`

**Ellis tab column headers (11 columns, `row_id` first for AppSheet
compatibility):**
```
row_id, paddock_name, date_in, date_out, head_count, class_of_stock,
days_rest, residual_note, weeds, wet_dry, notes
```

**KNOWN LIMITATION — creating brand-new sheets fails (403 PERMISSION_DENIED):**
Confirmed both Sheets API and Drive API are enabled on the right project
(`herdmate-uhf-tag-scanner`), so it's not an API-enablement issue. Most
likely cause: bare service accounts typically have no personal Drive
storage of their own, so creating an entirely new file fails even though
editing a file a real person already owns works fine. **Adding a tab to an
existing sheet is proven working end-to-end** (real onboarding session →
real answers → real Sheet tab created → real completion reported back).
Creating a sheet from scratch needs a real decision (Google Workspace
upgrade, a Shared Drive owned by a human with the service account added as
a member, or some other approach) — not a quick code fix. Don't spend more
time patching this without picking a direction first.

## Known open items (not yet built)

- **Billing/payments** — `plan` field exists, nothing charges anyone yet
- **Brand-new Google Sheet creation** — see limitation above
- **Ellis writing grazing records into the Sheet from a normal chat
  conversation** (the "tell Ellis what you did, it fills out the form"
  idea) — not started. The provisioner only builds the Sheet's structure;
  nothing yet reads a conversation and writes a row into it.
- **Unifying the four HTML tools** — shared login, shared data, cross-links
- **Other planned tools** (not started): paddock size planner, stockpile
  grazing planner, water system sizer, BCS-to-feeding-target converter,
  weed/toxic plant ID flow (as a dedicated tool, distinct from the chat
  skill which already exists), fence/corral spec helper, seasonal reminder
  generator
- **Grok's broader teacher-pack proposal** — tagged ChromaDB collections,
  ~25 additional PDFs, was reviewed and partially adopted (the 8 skills
  and system prompt lines are in; the collection restructuring and PDF
  batch were deferred, not done)
- **Light/dark mode** — untouched

## Troubleshooting things that have already bitten us once

- **`anthropic` + `httpx` version mismatch** → `TypeError: Client.__init__()
  got an unexpected keyword argument 'proxies'`. Fixed by pinning
  `anthropic==0.39.0` and `httpx==0.27.2`. Don't loosen without testing.
- **Disk fills up during build** → `No space left on device`. Caused by
  `sentence-transformers` pulling the full GPU build of torch (several GB
  of unneeded NVIDIA/CUDA packages on a droplet with no GPU). Fixed by
  installing CPU-only torch first in the Dockerfile, from
  `https://download.pytorch.org/whl/cpu`, before the rest of
  `requirements.txt`. Also: `docker system prune -a -f` reclaims old build
  cache/images if disk is already tight (safe, doesn't touch the
  persistent volume or running containers).
- **ChromaDB "connection failed, falling back to local storage"** — this is
  expected, not an error. No shared ChromaDB service exists on this server;
  each assistant runs its own embedded instance.
- **Copying a pre-built ChromaDB folder between environments** can crash
  with version mismatches. Always run ingest fresh in the actual serving
  environment.
- **Missing `google-auth` package** → `ModuleNotFoundError: No module named
  'google.oauth2'`. Add `google-auth==2.35.0` to `requirements.txt`.
- **Wrong credentials file path** — early guess was `/root/credentials.json`;
  the real path (matching Dave's actual setup) is
  `/app/ellis_knowledge_db/credentials.json`, i.e. on the persistent
  volume, set via `GOOGLE_CREDENTIALS_FILE`. Confirmed by inspecting Dave's
  container directly (`docker inspect <id> --format '{{ json .Config.Env }}'`)
  rather than guessing a second time.
- **Provisioner finds "0 pending sessions" when one is clearly waiting** —
  check the status filter. Sessions with a required EXTERNAL requirement
  still pending sit at `BLOCKED`, not `COMPLETED` — they can never reach
  COMPLETED until something (the provisioner) satisfies that requirement.
  Filtering for `status=COMPLETED` will silently find nothing, forever.
- **Uploaded a fix, redeployed, bug still there** — happened at least once.
  Always verify with `docker exec -it <id> grep ...` that the deployed
  code actually matches what was meant to be uploaded before assuming a
  fix didn't work.
- **`docker exec` "No such container"** — container IDs change on every
  redeploy. Always re-run `docker ps | grep -i ellis` before reusing an
  old ID from earlier in a session.

