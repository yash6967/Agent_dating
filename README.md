# Between Agents

A Python and Streamlit prototype for evidence-backed profile analysis, simulated agent dates, and ranked recommendations. It is isolated from the existing company website in the parent directory.

## Operator flow

1. Paste one pair or bulk import 25 comma-separated LinkedIn `/in/` and public Instagram profile URL pairs. No member accounts are needed for this POC.
2. Start **Analyze cohort**. A persistent background run calls two Apify Actors per person, rejects a private Instagram, checks that the account names plausibly refer to one person, and creates one agent per ready profile. A curator can review name mismatches.
3. The profile page shows supported observations under needs, hobbies, interests, values, style, and work. Every observation has a source link and excerpt. If something is not stated, it stays unknown.
4. Start **Autonomous dating**. The planner selects a bounded set of pairs that covers every ready agent; no operator chooses individual dates. Each eight-turn conversation is saved as it appears and can be watched or replayed.
5. Open a person's ranked list. For the initial 25-person cohort, every other ready profile is ranked. Above 200 people, SQLite FTS5 retrieves a bounded candidate set before scoring. A production deployment can replace this with a vector index and learned reciprocal ranking.

## Agent flow

```text
LinkedIn URL ── Apify ── snapshot ┐
                                  ├─ identity check ─ evidence claims ─ profile version
Instagram URL ─ Apify ── snapshot ┘                       │
                                                        ├─ candidate retrieval → ranking
                                                        └─ Agent A ↔ Agent B → saved date → ranking update
```

An agent receives only its person's LinkedIn and Instagram evidence, plus the shared date transcript. Date messages are **simulations**, not statements by the humans. Date transcripts never become facts about a person.

## Refresh policy

The database stores a separate `next_due_at` for each source. This operator-run 25-person POC checks all sources daily; it has no member activity signal. An operator can force a profile refresh. `refresh_due.py` is a daily scheduler entry point. The application compares normalized content hashes and analyzes a person again only when source content changes.

## Run locally

Python 3.12 is recommended on this Windows machine.

```powershell
cd agentic-dating
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APIFY_TOKEN = "your-apify-token"
$env:OPENAI_API_KEY = "your-model-api-key"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Alternatively, place `APIFY_TOKEN`, `OPENAI_API_KEY`, and optionally `OPENAI_MODEL` in `agentic-dating/.env` or `.streamlit/secrets.toml`. Both secret files and the SQLite database under `data/` are ignored by Git. Restart Streamlit after editing `.env`.

The default model is `gpt-6-luna`, with no reasoning tokens and bounded JSON output for profile analysis and dates. Set `OPENAI_MODEL` to override it. A completed date uses eight agent turns plus one evaluator request; the planner limits how many pairs reach that stage. A cheaper model reduces usage cost, but an API account with zero remaining credits still cannot run model analysis.

Without an Apify token, the app accepts links but cannot read them. Autonomous runs require both the Apify and model keys; they do not silently substitute mock output. Set `APIFY_LINKEDIN_ACTOR` or `APIFY_INSTAGRAM_ACTOR` to replace the default community Actors. The defaults are `calm_builder/linkedin-profile-scraper` and `calm_builder/instagram-profile-scraper`; test them with the intended cohort before relying on their field coverage or price.

Run the refresh job from Task Scheduler or a server cron once a day:

```powershell
.\.venv\Scripts\python.exe refresh_due.py --limit 100
```

Run the focused backend tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Current boundaries

- The implementation does not scrape private Instagram content or infer relationship intent, age, orientation, or intimate needs from public profiles.
- It stores only LinkedIn and Instagram material as evidence about a person. Operator actions and simulated dates do not add biographical claims.
- Account-name similarity is only a screening check. A human curator must resolve ambiguous identity pairs.
- The current recommendation score measures overlap in supported public-profile themes. It cannot predict attraction. A real dating product needs opted-in members and first-party preferences before making genuine introductions.
- SQLite and a single-process background thread suit a small research cohort. A production deployment should use a durable task queue, PostgreSQL, enforce user authorization, and add a vector retrieval service and ranking evaluation.
- The 25-person showcase and its three-minute video require verified public profile pairs and working Apify/model credentials. The app does not invent profiles when those inputs are unavailable.
