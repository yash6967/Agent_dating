"""Streamlit front end for the evidence-backed agent dating prototype."""

from __future__ import annotations

import json
import os
import csv
import io

import streamlit as st

from core import (
    active_model,
    add_person,
    agent_for,
    analyze_person,
    confirm_identity,
    connect,
    date_turns,
    due_sources,
    get_evidence,
    latest_profile,
    latest_ranking,
    list_dates,
    list_people,
    person,
    profile_claims,
    rank_for,
    ranking_stale,
    ranking_items,
    sources,
)
from autonomy import enqueue, ensure_worker, recent_runs, plan_for_run


st.set_page_config(page_title="Between Agents", page_icon="✦", layout="wide")

try:
    for secret_name in ("APIFY_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL"):
        if secret_name in st.secrets and not os.getenv(secret_name):
            os.environ[secret_name] = str(st.secrets[secret_name])
except Exception:
    pass

ensure_worker()

st.markdown(
    """
    <style>
    :root { --ink:#1d2830; --muted:#657178; --cream:#f7f5ef; --line:#deded7; --teal:#126b65; }
    .stApp { background:var(--cream); color:var(--ink); }
    .block-container { max-width:1240px; padding-top:2.4rem; }
    h1,h2,h3 { letter-spacing:-.035em; }
    h1 { font-size:3.2rem!important; line-height:1.04!important; }
    [data-testid="stSidebar"] { background:#e9ede8; border-right:1px solid var(--line); }
    div.stButton>button[kind="primary"] { background:var(--teal); border-color:var(--teal); }
    .eyebrow { color:var(--teal); font-size:.75rem; font-weight:750; letter-spacing:.16em; text-transform:uppercase; }
    .lede { color:var(--muted); max-width:52rem; font-size:1.08rem; }
    .soft-note { background:#e9ede8; padding:1rem 1.2rem; border-radius:.7rem; border:1px solid #d6ddd7; }
    </style>
    """,
    unsafe_allow_html=True,
)


def db():
    return connect()


def header(number: str, title: str, subtitle: str):
    st.markdown(f'<div class="eyebrow">{number} / BETWEEN AGENTS</div>', unsafe_allow_html=True)
    st.title(title)
    st.markdown(f'<p class="lede">{subtitle}</p>', unsafe_allow_html=True)


def _people_options(rows):
    return {f"{row['display_name']} · {row['status']} · {row['id'][:6]}": row["id"] for row in rows}


def _render_claims(connection, profile):
    claims = profile_claims(connection, profile["id"])
    evidence = get_evidence(connection, profile)
    st.write(profile["summary"])
    categories = [("need", "Needs"), ("hobby", "Hobbies"), ("interest", "Interests"),
                  ("value", "Values"), ("style", "Style"), ("work", "Work")]
    for category, heading in categories:
        selected = [row for row in claims if row["category"] == category]
        st.subheader(heading)
        if not selected:
            st.caption("No supported observation from the two public profiles.")
            continue
        for claim in selected:
            source = evidence.get(claim["evidence_key"])
            if source:
                st.markdown(f"**{claim['claim']}**  ·  [Source: {source['platform']}]({source['url']})")
                st.caption(f"Evidence: {source['text'][:240]} · confidence {claim['confidence']:.0%}")
    unknowns = json.loads(profile["unknowns_json"])
    if unknowns:
        st.info("Still unknown: " + "; ".join(unknowns))


@st.fragment(run_every="3s")
def run_status(kind: str | None = None):
    connection = db()
    try:
        runs = [r for r in recent_runs(connection, 5) if kind is None or r["kind"] == kind]
        for run in runs[:1]:
            label = "Profile analysis" if run["kind"] == "analyze_cohort" else "Autonomous dating"
            st.markdown(f"**{label}** · {run['status']} · {run['phase']}")
            total = run["progress_total"] or 0
            if total:
                st.progress(min(1.0, run["progress_done"] / total), text=f"{run['progress_done']} / {total} steps")
            for error in json.loads(run["error_json"])[-3:]:
                st.warning(error)
            if run["kind"] == "date_cohort":
                plan = plan_for_run(connection, run["id"])
                if plan:
                    st.dataframe([{"date": f"{p['person_a_name']} × {p['person_b_name']}",
                                   "status": p["status"], "retrieval score": round(p["pre_score"], 1)}
                                  for p in plan], hide_index=True, width="stretch")
    finally:
        connection.close()


@st.fragment(run_every="3s")
def date_replay():
    connection = db()
    try:
        available = [row for row in list_dates(connection) if row["status"] in ("complete", "running")]
        if not available:
            st.info("Dates will appear here as the agents begin speaking.")
            return
        date_labels = {f"{d['person_a_name']} × {d['person_b_name']} · {d['status']} · {d['created_at']}": d for d in available}
        selected_id = st.session_state.get("selected_date")
        date_names = list(date_labels)
        selected_index = next((i for i, name in enumerate(date_names) if date_labels[name]["id"] == selected_id), 0)
        selected_name = st.selectbox("Watch or replay a date", date_names, index=selected_index)
        selected = date_labels[selected_name]
        st.session_state["selected_date"] = selected["id"]
        st.caption(f"Engine: {selected['engine']} · {selected['created_at']} · saved profile versions {selected['profile_a_id'][:8]} / {selected['profile_b_id'][:8]}")
        for turn in date_turns(connection, selected["id"]):
            with st.chat_message("assistant", avatar="✨"):
                st.caption(f"Agent for {turn['display_name']} · {turn['phase']}")
                st.write(turn["text"])
                keys = json.loads(turn["evidence_keys_json"])
                if keys:
                    st.caption("Grounded in: " + ", ".join(keys))
        evaluation = json.loads(selected["evaluation_json"] or "{}")
        if evaluation.get("summary"):
            st.info(evaluation["summary"])
            st.caption(f"Conversation quality: {evaluation.get('conversation_score', 0):.0%} · " + " · ".join(evaluation.get("reasons", [])))
    finally:
        connection.close()


def add_page():
    header("01", "Build the cohort.", "Add one or many LinkedIn and public Instagram pairs. The operator starts a background read that creates one agent per verified person.")
    count_db = db()
    try:
        cohort = list_people(count_db)
        a, b, c = st.columns(3)
        a.metric("Pairs saved", len(cohort))
        b.metric("Agents ready", sum(p["status"] == "ready" and agent_for(count_db, p["id"]) is not None for p in cohort))
        c.metric("Awaiting review", sum(p["status"] == "review" for p in cohort))
    finally:
        count_db.close()
    left, right = st.columns([1.5, 1], gap="large")
    with left:
        with st.form("add_person", clear_on_submit=True):
            linkedin = st.text_input("LinkedIn public profile", placeholder="https://www.linkedin.com/in/...")
            instagram = st.text_input("Instagram public profile", placeholder="https://www.instagram.com/...")
            submitted = st.form_submit_button("Add pair", type="primary", width="stretch")
        if submitted:
            connection = db()
            try:
                person_id = add_person(connection, linkedin, instagram)
                st.session_state["selected_person"] = person_id
                st.success("Pair saved. Start cohort analysis when the list is ready.")
            except Exception as exc:
                st.error(str(exc))
            finally:
                connection.close()
        with st.form("bulk_add", clear_on_submit=True):
            bulk = st.text_area("Add many pairs", placeholder="https://www.linkedin.com/in/person-one/,https://www.instagram.com/personone/\nhttps://www.linkedin.com/in/person-two/,https://www.instagram.com/persontwo/", height=160)
            bulk_submit = st.form_submit_button("Import pairs")
        if bulk_submit:
            connection = db()
            added, errors = 0, []
            try:
                for line_number, pair in enumerate(csv.reader(io.StringIO(bulk)), 1):
                    if not pair or not any(part.strip() for part in pair):
                        continue
                    try:
                        if len(pair) != 2:
                            raise ValueError("Expected exactly two comma-separated URLs")
                        add_person(connection, pair[0].strip(), pair[1].strip())
                        added += 1
                    except Exception as exc:
                        errors.append(f"Line {line_number}: {exc}")
                st.success(f"Saved {added} pairs.")
                for error in errors[:8]:
                    st.warning(error)
            finally:
                connection.close()
        if st.button("Analyze cohort", type="primary", width="stretch"):
            try:
                enqueue("analyze_cohort", {"force": False})
                st.success("Background analysis started. Progress appears below.")
            except Exception as exc:
                st.error(str(exc))
        run_status("analyze_cohort")
    with right:
        st.markdown('<div class="soft-note"><b>Operator flow</b><br>01 · Add the 25 pairs<br>02 · Analyze the cohort<br>03 · Inspect profiles and evidence<br>04 · Run autonomous dates<br>05 · Explore rankings for every person</div>', unsafe_allow_html=True)
        st.write("")
        st.caption("Dates are simulations between agents. A public profile does not indicate that its owner wants to date.")
        st.caption(f"Analysis and date model: {active_model()}")
        if not os.getenv("APIFY_TOKEN"):
            st.warning("Apify token is not configured on this server.")
        if not os.getenv("OPENAI_API_KEY"):
            st.warning("OpenAI model key is not configured. Autonomous cohort runs require it.")


def profiles_page():
    header("02", "Profiles with receipts.", "Every observation links to the exact LinkedIn or Instagram content that supported it. Unknowns stay visible.")
    connection = db()
    try:
        rows = list_people(connection)
        if not rows:
            st.info("No people yet. Add a LinkedIn and Instagram pair to begin.")
            return
        options = _people_options(rows)
        chosen = st.session_state.get("selected_person")
        labels = list(options)
        index = next((i for i, label in enumerate(labels) if options[label] == chosen), 0)
        label = st.selectbox("Person", labels, index=index)
        person_id = options[label]
        st.session_state["selected_person"] = person_id
        row = person(connection, person_id)
        source_rows = sources(connection, person_id)
        left, right = st.columns([2.1, 1], gap="large")
        with right:
            st.subheader("Source status")
            for platform in ("linkedin", "instagram"):
                source = source_rows[platform]
                st.markdown(f"**{platform.title()}** · {source['status']}")
                st.markdown(f"[Open official profile]({source['url']})")
                if source["last_checked_at"]:
                    st.caption(f"Checked {source['last_checked_at']}")
                if source["error"]:
                    st.error(source["error"])
            if row["identity_score"] is not None:
                st.metric("Name agreement", f"{row['identity_score']:.0%}")
            st.caption("Name agreement is a screening check, not proof that the accounts belong to one person.")
            if row["status"] == "review" and not row["identity_verified"]:
                if st.button("Curator: confirm these are one person", width="stretch"):
                    confirm_identity(connection, person_id)
                    analyze_person(connection, person_id)
                    st.rerun()
            agent = agent_for(connection, person_id)
            st.caption(f"Agent: {agent['status'] if agent else 'pending'}")
            if st.button("Refresh & analyze", width="stretch"):
                try:
                    enqueue("analyze_cohort", {"person_ids": [person_id], "force": True})
                    st.success("Refresh queued. Progress is on the Add links page.")
                except Exception as exc:
                    st.error(str(exc))
        with left:
            profile = latest_profile(connection, person_id)
            if not profile:
                st.info("The links are saved. Read both public sources to create the profile page.")
                return
            st.caption(f"Analysis engine: {profile['analyzer']} · version {profile['id'][:8]} · {profile['generated_at']}")
            _render_claims(connection, profile)
    finally:
        connection.close()


def dates_page():
    header("03", "Watch the agents date.", "The planner chooses a bounded set of pairs covering every agent. Dates run in the background, then the app saves their conversations and rankings.")
    connection = db()
    try:
        ready = [row for row in list_people(connection) if row["status"] == "ready"]
        if len(ready) < 2:
            st.info("Analyze at least two people to run a date.")
            return
        st.metric("Ready agents", len(ready))
        per_agent = st.slider("Target dates per agent", 1, min(3, len(ready) - 1), 1)
        if st.button("Start autonomous dating", type="primary"):
            try:
                enqueue("date_cohort", {"dates_per_agent": per_agent})
                st.success("The planner is selecting dates. Progress appears below.")
            except Exception as exc:
                st.error(str(exc))
        run_status("date_cohort")
        date_replay()
    finally:
        connection.close()


def rankings_page():
    header("04", "A ranking for each person.", "Candidates are retrieved from analyzed profiles, scored on source-backed overlap, and refined when there is a saved agent date.")
    connection = db()
    try:
        ready = [row for row in list_people(connection) if row["status"] == "ready"]
        if not ready:
            st.info("Analyze a profile to see rankings.")
            return
        options = _people_options(ready)
        labels = list(options)
        selected = st.session_state.get("selected_person")
        index = next((i for i, name in enumerate(labels) if options[name] == selected), 0)
        label = st.selectbox("Ranked for", labels, index=index)
        person_id = options[label]
        if ranking_stale(connection, person_id):
            rank_for(connection, person_id)
        if st.button("Recompute ranking", type="primary"):
            rank_for(connection, person_id)
            st.rerun()
        run = latest_ranking(connection, person_id)
        if not run:
            st.info("No ranking yet. Recompute to generate one from the current cohort.")
            return
        st.caption(f"Generated {run['generated_at']} · algorithm {run['algorithm']} · profile version {run['profile_version_id'][:8]}")
        items = ranking_items(connection, run["id"])
        if not items:
            st.info("Add and analyze more people to make comparisons.")
        for item in items:
            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"### {item['position']:02d}  {item['display_name']}")
                c2.metric("Evidence fit", f"{item['score']:.1f}")
                for reason in json.loads(item["reasons_json"]):
                    st.caption(reason)
                if item["date_id"]:
                    st.caption(f"Agent date: {item['date_id'][:8]}")
    finally:
        connection.close()


def operations_page():
    header("05", "Refresh operations.", "For this 25-person POC, every source is due daily. Unchanged content keeps its existing profile version.")
    connection = db()
    try:
        due = due_sources(connection)
        st.metric("Sources due now", len(due))
        if due:
            st.dataframe([{"platform": row["platform"], "url": row["url"], "due": row["next_due_at"], "status": row["status"]} for row in due], width="stretch")
        if st.button("Refresh due sources", type="primary"):
            try:
                enqueue("analyze_cohort", {"force": False})
                st.success("Refresh queued.")
            except Exception as exc:
                st.error(str(exc))
        run_status("analyze_cohort")
    finally:
        connection.close()


with st.sidebar:
    st.markdown("### ✦ Between Agents")
    st.caption("PUBLIC PROFILES → EVIDENCE → DATES → RANKINGS")
    st.divider()
    st.caption("Research prototype · simulated dates")

add_page_ref = st.Page(add_page, title="Add links", icon=":material/add_link:", default=True)
profile_page_ref = st.Page(profiles_page, title="Profiles", icon=":material/person_search:")
date_page_ref = st.Page(dates_page, title="Agent dates", icon=":material/forum:")
ranking_page_ref = st.Page(rankings_page, title="Rankings", icon=":material/leaderboard:")
operations_page_ref = st.Page(operations_page, title="Refresh", icon=":material/sync:")

page = st.navigation([add_page_ref, profile_page_ref, date_page_ref, ranking_page_ref, operations_page_ref])
page.run()
