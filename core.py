"""Storage, source normalization, profile analysis, dates, and recommendations.

Only LinkedIn and Instagram snapshots contribute facts about a person. Dates are
simulations; their transcript is kept separate from source evidence.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path



ROOT = Path(__file__).resolve().parent


def load_local_env() -> None:
    """Read simple KEY=VALUE secrets without printing or overriding process settings."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"APIFY_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL", "APIFY_LINKEDIN_ACTOR", "APIFY_INSTAGRAM_ACTOR", "DATING_DB_PATH"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env()
DB_PATH = Path(os.getenv("DATING_DB_PATH", ROOT / "data" / "dating.sqlite3"))
ACTORS = {
    "linkedin": os.getenv("APIFY_LINKEDIN_ACTOR", "calm_builder/linkedin-profile-scraper"),
    "instagram": os.getenv("APIFY_INSTAGRAM_ACTOR", "calm_builder/instagram-profile-scraper"),
}
STOPWORDS = {
    "about", "across", "after", "also", "and", "are", "been", "being", "for", "from",
    "have", "into", "more", "our", "over", "that", "their", "them", "this", "with",
    "work", "working", "your", "the", "who", "was", "they", "will", "you", "but",
    "its", "his", "her", "she", "him", "any", "not", "all", "one", "out", "can",
}
CATEGORIES = {"interest", "hobby", "need", "value", "style", "work"}
DEFAULT_OPENAI_MODEL = "gpt-6-luna"


def active_model() -> str:
    return os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    people_columns = {row["name"] for row in db.execute("PRAGMA table_info(people)")}
    if "identity_verified" not in people_columns:
        db.execute("ALTER TABLE people ADD COLUMN identity_verified INTEGER NOT NULL DEFAULT 0")
    ranking_columns = {row["name"] for row in db.execute("PRAGMA table_info(ranking_runs)")}
    if "cohort_version" not in ranking_columns:
        db.execute("ALTER TABLE ranking_runs ADD COLUMN cohort_version INTEGER NOT NULL DEFAULT 0")
    return db


def canonical_url(value: str, platform: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Use a full https:// profile URL.")
    if platform == "linkedin":
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")) or len(parts) != 2 or parts[0] != "in":
            raise ValueError("LinkedIn must be a public linkedin.com/in/... person profile.")
        return f"https://www.linkedin.com/in/{parts[1]}/"
    if host not in {"instagram.com", "www.instagram.com"} or len(parts) != 1:
        raise ValueError("Instagram must be an instagram.com/<handle>/ profile.")
    if parts[0].lower() in {"p", "reel", "stories", "explore", "accounts"}:
        raise ValueError("Use an Instagram profile, not a post or feature URL.")
    return f"https://www.instagram.com/{parts[0].lower()}/"


def add_person(db: sqlite3.Connection, linkedin: str, instagram: str) -> str:
    urls = {"linkedin": canonical_url(linkedin, "linkedin"), "instagram": canonical_url(instagram, "instagram")}
    duplicate = db.execute("SELECT person_id FROM profile_sources WHERE url IN (?, ?)", tuple(urls.values())).fetchone()
    if duplicate:
        raise ValueError("One of these profiles is already in the cohort.")
    person_id = uid()
    label = urls["linkedin"].rstrip("/").split("/")[-1].replace("-", " ").title()
    stamp = now()
    with db:
        db.execute(
            "INSERT INTO people(id, display_name, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (person_id, label, stamp, stamp),
        )
        for platform, url in urls.items():
            db.execute(
                "INSERT INTO profile_sources(id, person_id, platform, url, next_due_at) VALUES(?, ?, ?, ?, ?)",
                (uid(), person_id, platform, url, stamp),
            )
    return person_id


def list_people(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM profile_versions v WHERE v.person_id=p.id) AS versions
           FROM people p ORDER BY p.created_at DESC"""
    ).fetchall()


def person(db: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()


def sources(db: sqlite3.Connection, person_id: str) -> dict[str, sqlite3.Row]:
    return {row["platform"]: row for row in db.execute("SELECT * FROM profile_sources WHERE person_id=?", (person_id,))}


def latest_snapshot(db: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM source_snapshots WHERE source_id=? ORDER BY observed_at DESC LIMIT 1", (source_id,)
    ).fetchone()


def latest_profile(db: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM profile_versions WHERE person_id=? ORDER BY generated_at DESC LIMIT 1", (person_id,)
    ).fetchone()


def profile_claims(db: sqlite3.Connection, profile_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM evidence_claims WHERE profile_version_id=? ORDER BY category, confidence DESC", (profile_id,)
    ).fetchall()


def _request_json(url: str, *, token: str | None = None, payload: dict | None = None, timeout: int = 40):
    headers = {"Accept": "application/json", "User-Agent": "AgenticDating/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def run_actor(actor_id: str, payload: dict, token: str) -> tuple[str, list[dict]]:
    actor_path = urllib.parse.quote(actor_id.replace("/", "~"), safe="~")
    run = _request_json(f"https://api.apify.com/v2/acts/{actor_path}/runs", token=token, payload=payload)
    run_data = run.get("data", {})
    run_id = run_data.get("id")
    if not run_id:
        raise RuntimeError("Apify did not return an Actor run ID.")
    deadline = time.monotonic() + 210
    while time.monotonic() < deadline:
        run_data = _request_json(f"https://api.apify.com/v2/actor-runs/{run_id}", token=token).get("data", {})
        status = run_data.get("status")
        if status == "SUCCEEDED":
            dataset_id = run_data.get("defaultDatasetId")
            if not dataset_id:
                raise RuntimeError("Apify completed without a dataset.")
            items = _request_json(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items?format=json&clean=true", token=token
            )
            if not isinstance(items, list):
                raise RuntimeError("Apify returned an unexpected dataset format.")
            return run_id, items
        if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
            raise RuntimeError(f"Apify run {run_id} ended with {status}.")
        time.sleep(3)
    raise TimeoutError(f"Apify run {run_id} is still in progress. Retry refresh later.")


def _clean_text(value: object, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def normalize_confidence(value: object) -> float:
    """Accept model confidence labels and numbers without breaking ingestion."""
    if isinstance(value, bool) or value is None:
        return 0.5
    if isinstance(value, str):
        normalized = value.strip().lower()
        labels = {"very high": 0.95, "high": 0.85, "medium": 0.6,
                  "moderate": 0.6, "low": 0.35, "very low": 0.2}
        if normalized in labels:
            return labels[normalized]
        if normalized.endswith("%"):
            try:
                value = float(normalized[:-1]) / 100
            except ValueError:
                return 0.5
        else:
            value = normalized
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.5


def normalize(platform: str, item: dict, url: str) -> dict:
    if platform == "linkedin":
        return {
            "name": _clean_text(item.get("fullName") or item.get("name"), 120),
            "headline": _clean_text(item.get("headline"), 300),
            "about": _clean_text(item.get("about"), 3000),
            "location": _clean_text(item.get("location"), 120),
            "experience": [
                {"title": _clean_text(x.get("title"), 180), "company": _clean_text(x.get("company"), 180),
                 "description": _clean_text(x.get("description"), 1000)}
                for x in (item.get("experience") or [])[:12] if isinstance(x, dict)
            ],
            "skills": [_clean_text(x, 100) for x in (item.get("skills") or [])[:30] if isinstance(x, str)],
            "posts": [
                {"text": _clean_text(x.get("text") or x.get("content") or x.get("description"), 1200),
                 "url": x.get("url") if isinstance(x.get("url"), str) else url}
                for x in (item.get("posts") or [])[:10] if isinstance(x, dict)
            ],
            "url": url,
        }
    posts = item.get("latestPosts") or item.get("posts") or []
    return {
        "name": _clean_text(item.get("fullName") or item.get("full_name"), 120),
        "username": _clean_text(item.get("username"), 80),
        "biography": _clean_text(item.get("biography") or item.get("bio"), 2000),
        "private": item.get("private", item.get("is_private", item.get("isPrivate"))),
        "verified": bool(item.get("verified", item.get("is_verified", False))),
        "posts": [
            {"text": _clean_text(x.get("caption"), 1200),
             "url": x.get("url") if isinstance(x.get("url"), str) else url}
            for x in posts[:12] if isinstance(x, dict)
        ],
        "url": url,
    }


def _refresh_interval() -> timedelta:
    """A small operator-run cohort can refresh every public source daily."""
    return timedelta(days=1)


def confirm_identity(db: sqlite3.Connection, person_id: str) -> None:
    """Curator action for different public display names; not an automated proof."""
    with db:
        db.execute("UPDATE people SET identity_verified=1, updated_at=? WHERE id=?", (now(), person_id))


def due_sources(db: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM profile_sources WHERE next_due_at IS NOT NULL AND next_due_at<=? ORDER BY next_due_at LIMIT ?",
        (now(), limit),
    ).fetchall()


def refresh_source(db: sqlite3.Connection, source_id: str, token: str) -> bool:
    source = db.execute("SELECT * FROM profile_sources WHERE id=?", (source_id,)).fetchone()
    if not source:
        raise ValueError("Source not found.")
    platform = source["platform"]
    actor = ACTORS[platform]
    payload = (
        {"profiles": [source["url"]]}
        if platform == "linkedin" else
        {"usernames": [source["url"]], "includeRelatedProfiles": False, "includeLatestPosts": True}
    )
    try:
        run_id, items = run_actor(actor, payload, token)
        if not items:
            raise RuntimeError("The Actor returned no public profile for this URL.")
        content = normalize(platform, items[0], source["url"])
        if platform == "instagram" and content["private"] is None:
            raise RuntimeError("The Instagram Actor did not report public/private status; cannot verify this profile is public.")
        if platform == "instagram" and content["private"]:
            with db:
                db.execute(
                    "UPDATE profile_sources SET status='private', error='Instagram is private', last_checked_at=?, next_due_at=NULL WHERE id=?",
                    (now(), source_id),
                )
                db.execute("UPDATE people SET status='private', updated_at=? WHERE id=?", (now(), source["person_id"]))
                db.execute("UPDATE app_meta SET value=value+1 WHERE key='cohort_version'")
            return False
        if not content["name"]:
            raise RuntimeError("The Actor returned a profile without a display name.")
        serialized = json.dumps(content, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        previous = latest_snapshot(db, source_id)
        changed = previous is None or previous["content_hash"] != fingerprint
        checked = now()
        next_due = (datetime.now(timezone.utc) + _refresh_interval()).isoformat(timespec="seconds")
        with db:
            if changed:
                db.execute(
                    "INSERT INTO source_snapshots(id, source_id, observed_at, actor_id, apify_run_id, content_hash, content_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (uid(), source_id, checked, actor, run_id, fingerprint, serialized),
                )
            db.execute(
                "UPDATE profile_sources SET status='ready', error=NULL, last_checked_at=?, last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END, next_due_at=? WHERE id=?",
                (checked, int(changed), checked, next_due, source_id),
            )
        return changed
    except Exception as exc:
        retry_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(timespec="seconds")
        with db:
            db.execute(
                "UPDATE profile_sources SET status='error', error=?, last_checked_at=?, next_due_at=? WHERE id=?",
                (str(exc)[:500], now(), retry_at, source_id),
            )
        raise


def evidence_for(linkedin: dict, instagram: dict) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    def add(key: str, text: str, url: str, platform: str):
        if text:
            evidence[key] = {"text": text, "url": url, "platform": platform}
    add("li:headline", linkedin.get("headline", ""), linkedin["url"], "linkedin")
    add("li:about", linkedin.get("about", ""), linkedin["url"], "linkedin")
    for i, role in enumerate(linkedin.get("experience", [])):
        add(f"li:role:{i}", " — ".join(filter(None, [role["title"], role["company"], role["description"]])), linkedin["url"], "linkedin")
    for i, skill in enumerate(linkedin.get("skills", [])):
        add(f"li:skill:{i}", skill, linkedin["url"], "linkedin")
    for i, post in enumerate(linkedin.get("posts", [])):
        add(f"li:post:{i}", post["text"], post["url"], "linkedin")
    add("ig:bio", instagram.get("biography", ""), instagram["url"], "instagram")
    for i, post in enumerate(instagram.get("posts", [])):
        add(f"ig:post:{i}", post["text"], post["url"], "instagram")
    return evidence


def identity_score(linkedin_name: str, instagram_name: str) -> float:
    def cleaned(value: str) -> str:
        return re.sub(r"[^a-z ]", "", value.lower())
    return round(difflib.SequenceMatcher(None, cleaned(linkedin_name), cleaned(instagram_name)).ratio(), 3)


def _model_json(instructions: str, payload: dict, *, model: str | None = None,
                max_output_tokens: int = 1600) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    selected_model = model or active_model()
    request_payload = {
        "model": selected_model,
        "instructions": instructions,
        "input": "Return a JSON object using only this input:\n" + json.dumps(payload, ensure_ascii=False),
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    if selected_model == "gpt-6-luna":
        request_payload["reasoning"] = {"effort": "none"}
    for attempt in range(8):
        try:
            response = _request_json(
                "https://api.openai.com/v1/responses",
                token=api_key,
                payload=request_payload,
                timeout=120,
            )
            break
        except RuntimeError as exc:
            if "rate_limit_exceeded" not in str(exc) or attempt == 7:
                raise
            retry_hint = re.search(r"try again in ([\d.]+)s", str(exc), re.IGNORECASE)
            delay = min(30.0, max(2.0, float(retry_hint.group(1)) + 0.5)) if retry_hint else 8.0
            time.sleep(delay)
    output = "".join(
        part.get("text", "")
        for item in response.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if not output:
        raise RuntimeError("The model returned no text output.")
    return json.loads(output)


def _fallback_analysis(linkedin: dict, instagram: dict, evidence: dict[str, dict]) -> dict:
    claims = []
    for key in ("li:headline", "li:about", "ig:bio"):
        if key in evidence:
            claims.append({"category": "work" if key.startswith("li:") else "interest", "claim": evidence[key]["text"][:180], "evidence_key": key, "confidence": 0.6})
    for key in list(evidence):
        if key.startswith("ig:post:") and len(claims) < 8:
            claims.append({"category": "interest", "claim": evidence[key]["text"][:180], "evidence_key": key, "confidence": 0.5})
    return {
        "summary": f"Public-profile notes for {linkedin['name']}. These are observations, not verified dating preferences.",
        "claims": claims,
        "unknowns": ["Relationship intentions", "Dating preferences", "Personal needs not stated on either profile"],
    }


def analyze_person(db: sqlite3.Connection, person_id: str) -> str:
    src = sources(db, person_id)
    if set(src) != {"linkedin", "instagram"}:
        raise ValueError("A person needs exactly one LinkedIn and one Instagram profile.")
    if any(row["status"] != "ready" for row in src.values()):
        raise ValueError("Both public sources must be successfully checked before analysis.")
    snapshots = {platform: latest_snapshot(db, row["id"]) for platform, row in src.items()}
    if any(snapshot is None for snapshot in snapshots.values()):
        raise ValueError("Refresh both public sources before analysis.")
    linkedin = json.loads(snapshots["linkedin"]["content_json"])
    instagram = json.loads(snapshots["instagram"]["content_json"])
    if instagram.get("private"):
        raise ValueError("Private Instagram accounts cannot be analyzed.")
    score = identity_score(linkedin["name"], instagram["name"])
    owner = person(db, person_id)
    if score < 0.62 and not owner["identity_verified"]:
        with db:
            db.execute("UPDATE people SET status='review', identity_score=?, updated_at=? WHERE id=?", (score, now(), person_id))
        raise ValueError("The account names differ substantially. Review these links before treating them as one person.")
    evidence = evidence_for(linkedin, instagram)
    use_model = bool(os.getenv("OPENAI_API_KEY"))
    analyzer = f"openai:{active_model()}" if use_model else "rules:v1"
    existing = db.execute(
        """SELECT id FROM profile_versions WHERE person_id=? AND linkedin_snapshot_id=?
           AND instagram_snapshot_id=? AND analyzer=?""",
        (person_id, snapshots["linkedin"]["id"], snapshots["instagram"]["id"], analyzer),
    ).fetchone()
    if existing:
        with db:
            db.execute(
                "UPDATE people SET display_name=?, status='ready', identity_score=?, updated_at=? WHERE id=?",
                (linkedin["name"], score, now(), person_id),
            )
            db.execute(
                """INSERT INTO agents(id, person_id, current_profile_version_id, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET
                   current_profile_version_id=excluded.current_profile_version_id, updated_at=excluded.updated_at""",
                (uid(), person_id, existing["id"], now(), now()),
            )
        return existing["id"]
    if use_model:
        result = _model_json(
            "Return JSON with summary (string), claims (array of {category, claim, evidence_key, confidence}), "
            "and unknowns (array of strings). Analyze only the supplied LinkedIn and Instagram excerpts. "
            "Each claim must cite one provided evidence_key and have numeric confidence between 0 and 1. "
            "Use categories interest, hobby, need, value, style, work. "
            "Record a need only if explicitly stated. Do not infer relationship status, orientation, age, "
            "health, religion, or intimate preferences. Avoid certainty where the source is ambiguous.",
            {"linkedin_name": linkedin["name"], "instagram_name": instagram["name"], "evidence": evidence},
        )
    else:
        result = _fallback_analysis(linkedin, instagram, evidence)
    valid_claims = []
    claims = result.get("claims")
    for claim in (claims if isinstance(claims, list) else [])[:24]:
        if not isinstance(claim, dict):
            continue
        key = claim.get("evidence_key")
        category = claim.get("category")
        text = _clean_text(claim.get("claim"), 240)
        if key in evidence and category in CATEGORIES and text:
            valid_claims.append((category, text, key, normalize_confidence(claim.get("confidence"))))
    profile_id = uid()
    stamp = now()
    with db:
        db.execute(
            """INSERT INTO profile_versions(id, person_id, linkedin_snapshot_id, instagram_snapshot_id,
               generated_at, analyzer, summary, unknowns_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, person_id, snapshots["linkedin"]["id"], snapshots["instagram"]["id"], stamp,
             analyzer, _clean_text(result.get("summary"), 1000),
             json.dumps((result.get("unknowns") if isinstance(result.get("unknowns"), list) else [])[:10])),
        )
        db.executemany(
            "INSERT INTO evidence_claims(id, profile_version_id, category, claim, evidence_key, confidence) VALUES(?, ?, ?, ?, ?, ?)",
            [(uid(), profile_id, *claim) for claim in valid_claims],
        )
        db.execute(
            "UPDATE people SET display_name=?, status='ready', identity_score=?, updated_at=? WHERE id=?",
            (linkedin["name"], score, stamp, person_id),
        )
        db.execute(
            """INSERT INTO agents(id, person_id, current_profile_version_id, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET
               current_profile_version_id=excluded.current_profile_version_id, updated_at=excluded.updated_at""",
            (uid(), person_id, profile_id, stamp, stamp),
        )
        db.execute("DELETE FROM profile_search WHERE person_id=?", (person_id,))
        db.execute(
            "INSERT INTO profile_search(person_id, document) VALUES(?, ?)",
            (person_id, " ".join(claim[1] for claim in valid_claims)),
        )
        db.execute("UPDATE app_meta SET value=value+1 WHERE key='cohort_version'")
    return profile_id


def get_evidence(db: sqlite3.Connection, profile: sqlite3.Row) -> dict[str, dict]:
    li = db.execute("SELECT content_json FROM source_snapshots WHERE id=?", (profile["linkedin_snapshot_id"],)).fetchone()
    ig = db.execute("SELECT content_json FROM source_snapshots WHERE id=?", (profile["instagram_snapshot_id"],)).fetchone()
    return evidence_for(json.loads(li["content_json"]), json.loads(ig["content_json"]))


def agent_for(db: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM agents WHERE person_id=?", (person_id,)).fetchone()


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in STOPWORDS}


def _weighted_terms(db: sqlite3.Connection, profile_id: str) -> dict[str, float]:
    weights = {"need": 1.8, "value": 1.6, "hobby": 1.5, "interest": 1.4, "style": 1.2, "work": 0.5}
    terms: dict[str, float] = {}
    for claim in profile_claims(db, profile_id):
        strength = weights[claim["category"]] * claim["confidence"]
        for term in _tokens(claim["claim"]):
            terms[term] = max(terms.get(term, 0), strength)
    return terms


def _profile_terms(db: sqlite3.Connection, profile_id: str) -> set[str]:
    return set(_weighted_terms(db, profile_id))


def candidate_ids(db: sqlite3.Connection, person_id: str, limit: int = 200) -> list[str]:
    count = db.execute("SELECT COUNT(*) FROM people WHERE status='ready' AND id<>?", (person_id,)).fetchone()[0]
    if count <= limit:
        return [row[0] for row in db.execute("SELECT id FROM people WHERE status='ready' AND id<>?", (person_id,))]
    profile = latest_profile(db, person_id)
    if not profile:
        return []
    terms = sorted(_profile_terms(db, profile["id"]), key=len, reverse=True)[:12]
    retrieved = []
    if terms:
        query = " OR ".join(f'"{term}"' for term in terms)
        retrieved = [row["person_id"] for row in db.execute(
            """SELECT s.person_id FROM profile_search s JOIN people p ON p.id=s.person_id
               WHERE profile_search MATCH ? AND s.person_id<>? AND p.status='ready'
               ORDER BY bm25(profile_search) LIMIT ?""", (query, person_id, limit)
        )]
    if len(retrieved) < limit:
        excluded = [person_id, *retrieved]
        placeholders = ",".join("?" for _ in excluded)
        retrieved.extend(row["id"] for row in db.execute(
            f"SELECT id FROM people WHERE status='ready' AND id NOT IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
            (*excluded, limit - len(retrieved)),
        ))
    return retrieved


def pair_score(db: sqlite3.Connection, a_id: str, b_id: str) -> tuple[float, list[str]]:
    a = latest_profile(db, a_id)
    b = latest_profile(db, b_id)
    if not a or not b:
        return 0.0, ["One profile has not been analyzed"]
    a_terms = _weighted_terms(db, a["id"])
    b_terms = _weighted_terms(db, b["id"])
    shared = set(a_terms) & set(b_terms)
    shared_weight = sum(min(a_terms[t], b_terms[t]) for t in shared)
    a_fit = shared_weight / sum(a_terms.values()) if a_terms else 0.0
    b_fit = shared_weight / sum(b_terms.values()) if b_terms else 0.0
    reciprocal = 2 * a_fit * b_fit / (a_fit + b_fit) if a_fit + b_fit else 0.0
    evidence_factor = min(1.0, (len(a_terms) * len(b_terms)) ** 0.5 / 12)
    score = round(100 * (0.6 * reciprocal + 0.4 * a_fit) * evidence_factor, 1)
    shared_ranked = sorted(shared, key=lambda t: min(a_terms[t], b_terms[t]), reverse=True)
    reasons = [f"Shared source-backed themes: {', '.join(shared_ranked[:6])}"] if shared else ["Limited overlap in the available public evidence"]
    reasons.append(f"Directional theme coverage {a_fit:.0%}; reciprocal coverage {reciprocal:.0%}")
    reasons.append("This is an evidence similarity score, not a prediction of attraction")
    return score, reasons


def latest_date(db: sqlite3.Connection, a_id: str, b_id: str) -> sqlite3.Row | None:
    a_profile, b_profile = latest_profile(db, a_id), latest_profile(db, b_id)
    if not a_profile or not b_profile:
        return None
    return db.execute(
        """SELECT * FROM agent_dates WHERE status='complete' AND
           ((person_a_id=? AND person_b_id=? AND profile_a_id=? AND profile_b_id=?) OR
            (person_a_id=? AND person_b_id=? AND profile_a_id=? AND profile_b_id=?))
           ORDER BY created_at DESC LIMIT 1""",
        (a_id, b_id, a_profile["id"], b_profile["id"], b_id, a_id, b_profile["id"], a_profile["id"])
    ).fetchone()


def cohort_version(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT value FROM app_meta WHERE key='cohort_version'").fetchone()[0])


def ranking_stale(db: sqlite3.Connection, person_id: str) -> bool:
    run = latest_ranking(db, person_id)
    profile = latest_profile(db, person_id)
    return run is None or profile is None or run["profile_version_id"] != profile["id"] or run["cohort_version"] != cohort_version(db)


def rank_for(db: sqlite3.Connection, person_id: str) -> str:
    own = latest_profile(db, person_id)
    if not own:
        raise ValueError("Analyze this person before ranking.")
    rows = []
    for candidate_id in candidate_ids(db, person_id):
        score, reasons = pair_score(db, person_id, candidate_id)
        date = latest_date(db, person_id, candidate_id)
        if date:
            evaluation = json.loads(date["evaluation_json"] or "{}")
            score = round(min(100, score + 10 * float(evaluation.get("conversation_score", 0))), 1)
            reasons.append("Includes a saved agent date")
        rows.append((candidate_id, score, reasons, date["id"] if date else None))
    rows.sort(key=lambda row: (-row[1], row[0]))
    run_id = uid()
    with db:
        db.execute(
            "INSERT INTO ranking_runs(id, person_id, generated_at, profile_version_id, algorithm, cohort_version) VALUES(?, ?, ?, ?, 'evidence-v2', ?)",
            (run_id, person_id, now(), own["id"], cohort_version(db)),
        )
        db.executemany(
            "INSERT INTO ranking_items(run_id, candidate_id, position, score, reasons_json, date_id) VALUES(?, ?, ?, ?, ?, ?)",
            [(run_id, candidate_id, i, score, json.dumps(reasons), date_id) for i, (candidate_id, score, reasons, date_id) in enumerate(rows, 1)],
        )
    return run_id


def latest_ranking(db: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM ranking_runs WHERE person_id=? ORDER BY generated_at DESC LIMIT 1", (person_id,)).fetchone()


def ranking_items(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT i.*, p.display_name FROM ranking_items i JOIN people p ON p.id=i.candidate_id
           WHERE i.run_id=? ORDER BY i.position""", (run_id,)
    ).fetchall()


def _rules_turn(speaker: sqlite3.Row, other: sqlite3.Row, claims: list[sqlite3.Row], phase: str, index: int) -> tuple[str, list[str]]:
    chosen = claims[index % len(claims)] if claims else None
    observation = chosen["claim"] if chosen else "the public profiles give only a partial picture"
    key = [chosen["evidence_key"]] if chosen else []
    if phase == "opening":
        return f"I'm the agent for {speaker['display_name']}. One thing their public profile mentions is {observation}. What topic would you want us to explore first?", key
    if phase == "closing":
        return f"Thanks for the conversation. I noticed a useful point of connection, but I would ask {speaker['display_name']} directly before treating this as a real match.", key
    questions = [
        "What does that look like in your day-to-day life?",
        "Which part of that matters most to your person?",
        "Where might our interests differ?",
        "What should we leave as an open question for the people themselves?",
    ]
    return f"Their profile also mentions {observation}. {questions[index % len(questions)]}", key


def _evaluate_date(db: sqlite3.Connection, date_id: str, a: sqlite3.Row, b: sqlite3.Row, pa: sqlite3.Row, pb: sqlite3.Row, engine: str) -> dict:
    turns = date_turns(db, date_id)
    transcript = [
        {"speaker": t["display_name"], "phase": t["phase"], "text": t["text"],
         "evidence_keys": json.loads(t["evidence_keys_json"])} for t in turns
    ]
    if engine == "openai":
        result = _model_json(
            "You are an evaluator separate from the two dating agents. Return JSON with conversation_score "
            "(number 0 to 1), summary (string), and reasons (array of short strings). Evaluate only the "
            "quality of this simulated dialogue: responsiveness, thoughtful questions, grounded claims, "
            "and openly acknowledged unknowns. Do not predict human attraction or invent relationship intent.",
            {"agent_a": {"name": a["display_name"], "claims": [dict(c) for c in profile_claims(db, pa["id"])]},
             "agent_b": {"name": b["display_name"], "claims": [dict(c) for c in profile_claims(db, pb["id"])]},
             "transcript": transcript},
            max_output_tokens=360,
        )
        return {
            "conversation_score": round(min(1, max(0, float(result.get("conversation_score", 0)))), 2),
            "summary": _clean_text(result.get("summary"), 400),
            "reasons": [_clean_text(x, 180) for x in result.get("reasons", [])[:5] if isinstance(x, str)],
        }
    grounded = sum(bool(t["evidence_keys"]) for t in transcript)
    questions = sum("?" in t["text"] for t in transcript)
    continuity = sum(
        bool(_tokens(transcript[i]["text"]) & _tokens(transcript[i-1]["text"]))
        for i in range(1, len(transcript))
    )
    score = min(1.0, 0.15 + 0.3 * grounded / 8 + 0.2 * questions / 8 + 0.35 * continuity / 7)
    return {
        "conversation_score": round(score, 2),
        "summary": "A rule-based assessment of a simulated conversation. Human interest and chemistry remain unknown.",
        "reasons": [f"{grounded} of 8 turns cite profile evidence", f"{questions} turns ask a question", "Conversation continuity is estimated from shared terms"],
    }


def run_date(db: sqlite3.Connection, a_id: str, b_id: str, progress=None) -> str:
    if a_id == b_id:
        raise ValueError("Choose two different people.")
    a, b = person(db, a_id), person(db, b_id)
    pa, pb = latest_profile(db, a_id), latest_profile(db, b_id)
    if not a or not b or not pa or not pb:
        raise ValueError("Both people need analyzed profiles before a date.")
    engine = "openai" if os.getenv("OPENAI_API_KEY") else "rules"
    date_id = uid()
    with db:
        db.execute(
            "INSERT INTO agent_dates(id, person_a_id, person_b_id, profile_a_id, profile_b_id, created_at, engine) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (date_id, a_id, b_id, pa["id"], pb["id"], now(), engine),
        )
    phases = ["opening", "opening", "discovery", "discovery", "values", "values", "closing", "closing"]
    transcript = []
    try:
        for i, phase in enumerate(phases):
            speaker, other = (a, b) if i % 2 == 0 else (b, a)
            profile = pa if i % 2 == 0 else pb
            claims = profile_claims(db, profile["id"])
            allowed = {claim["evidence_key"] for claim in claims}
            if engine == "openai":
                result = _model_json(
                    "You are a clearly labeled simulated dating agent, not the human. Return JSON with text "
                    "(1-3 natural sentences) and evidence_keys (array). Speak to the other agent. "
                    "Use only facts from your own cited profile; do not invent feelings, relationship intent, "
                    "personal needs or attraction. Ask a thoughtful question when appropriate. "
                    "Treat the conversation as exploratory, not consent from the humans.",
                    {"phase": phase, "speaker": speaker["display_name"], "other_agent": other["display_name"],
                     "own_summary": profile["summary"], "own_claims": [dict(c) for c in claims],
                     "transcript": transcript},
                    max_output_tokens=300,
                )
                message = _clean_text(result.get("text"), 500)
                keys = [key for key in result.get("evidence_keys", []) if key in allowed]
                if not message:
                    raise RuntimeError("An agent returned an empty date turn.")
            else:
                message, keys = _rules_turn(speaker, other, claims, phase, i)
            transcript.append({"speaker": speaker["display_name"], "text": message, "phase": phase})
            with db:
                db.execute(
                    "INSERT INTO date_turns(id, date_id, turn_index, speaker_id, phase, text, evidence_keys_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (uid(), date_id, i, speaker["id"], phase, message, json.dumps(keys)),
                )
            if progress:
                progress(i + 1, len(phases), speaker["display_name"], message)
        evaluation = _evaluate_date(db, date_id, a, b, pa, pb, engine)
        with db:
            db.execute("UPDATE agent_dates SET status='complete', evaluation_json=? WHERE id=?", (json.dumps(evaluation), date_id))
        return date_id
    except Exception:
        with db:
            db.execute("UPDATE agent_dates SET status='error' WHERE id=?", (date_id,))
        raise


def date_turns(db: sqlite3.Connection, date_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT t.*, p.display_name FROM date_turns t JOIN people p ON p.id=t.speaker_id
           WHERE t.date_id=? ORDER BY t.turn_index""", (date_id,)
    ).fetchall()


def list_dates(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT d.*, a.display_name AS person_a_name, b.display_name AS person_b_name
           FROM agent_dates d JOIN people a ON a.id=d.person_a_id JOIN people b ON b.id=d.person_b_id
           ORDER BY d.created_at DESC LIMIT 100"""
    ).fetchall()


def plan_autonomous_pairs(db: sqlite3.Connection, dates_per_agent: int = 1, retrieval_limit: int = 80) -> list[tuple[str, str, float]]:
    """Greedy bounded edge cover: every ready agent gets at least one date.

    For the 25-person POC, all 300 possible pairs are cheap to score without
    involving the language model. Large cohorts only score retrieved edges.
    """
    if dates_per_agent < 1 or dates_per_agent > 3:
        raise ValueError("Choose between one and three dates per agent.")
    ready = [row["id"] for row in list_people(db) if row["status"] == "ready" and agent_for(db, row["id"])]
    if len(ready) < 2:
        raise ValueError("Analyze at least two people before starting autonomous dates.")
    if dates_per_agent >= len(ready):
        raise ValueError("Target dates per agent must be smaller than the ready cohort.")
    edge_scores: dict[tuple[str, str], float] = {}
    for owner_id in ready:
        for candidate_id in candidate_ids(db, owner_id, retrieval_limit):
            if candidate_id == owner_id:
                continue
            pair = tuple(sorted((owner_id, candidate_id)))
            if pair not in edge_scores:
                score_ab, _ = pair_score(db, pair[0], pair[1])
                score_ba, _ = pair_score(db, pair[1], pair[0])
                edge_scores[pair] = (score_ab + score_ba) / 2
    # Retrieval may return nothing for a sparse profile. Add a bounded fallback
    # so that its agent can still enter an exploratory date.
    connected = {pid for pair in edge_scores for pid in pair}
    for owner_id in ready:
        if owner_id not in connected:
            for other_id in ready:
                if other_id != owner_id:
                    pair = tuple(sorted((owner_id, other_id)))
                    edge_scores[pair] = 0.0
                    break
    degrees = {pid: 0 for pid in ready}
    max_degree = max(2, dates_per_agent + 1)
    selected: list[tuple[str, str, float]] = []
    remaining = dict(edge_scores)
    while any(degree < dates_per_agent for degree in degrees.values()):
        feasible = [
            (pair, score) for pair, score in remaining.items()
            if degrees[pair[0]] < max_degree and degrees[pair[1]] < max_degree
            and (degrees[pair[0]] < dates_per_agent or degrees[pair[1]] < dates_per_agent)
        ]
        if not feasible:
            raise RuntimeError("Could not cover every agent with the retrieved date candidates.")
        pair, score = max(
            feasible,
            key=lambda item: (
                int(degrees[item[0][0]] < dates_per_agent) + int(degrees[item[0][1]] < dates_per_agent),
                item[1],
            ),
        )
        selected.append((pair[0], pair[1], score))
        degrees[pair[0]] += 1
        degrees[pair[1]] += 1
        del remaining[pair]
    return selected
