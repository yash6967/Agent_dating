"""Run once daily from a scheduler to refresh sources that are due."""

from __future__ import annotations

import argparse
import os

from core import analyze_person, connect, due_sources, person, refresh_source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100, help="Maximum source URLs in this pass")
    args = parser.parse_args()
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        parser.error("APIFY_TOKEN is required")
    db = connect()
    changed_people = set()
    failed = 0
    try:
        for source in due_sources(db, args.limit):
            try:
                changed = refresh_source(db, source["id"], token)
                changed_people.update([source["person_id"]] if changed else [])
                print(f"checked {source['platform']} {source['url']} changed={changed}")
            except Exception as exc:
                failed += 1
                print(f"error {source['platform']} {source['url']}: {exc}")
        for person_id in changed_people:
            try:
                profile_id = analyze_person(db, person_id)
                print(f"analyzed {person(db, person_id)['display_name']} profile={profile_id}")
            except Exception as exc:
                failed += 1
                print(f"error profile {person_id}: {exc}")
    finally:
        db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

