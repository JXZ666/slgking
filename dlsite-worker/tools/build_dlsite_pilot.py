"""Build a sanitized local DLsite snapshot from verified public fact records."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import slg_db
import slg_dlsite

FIELDS = {"rating_value", "rating_count", "rating_scale_max", "work_type",
          "release_date", "updated_date", "languages", "age_rating"}

def build(facts_path, output, covers=None):
    facts = json.loads(Path(facts_path).read_text(encoding="utf-8"))
    if facts.get("catalog_id") != "dlsite" or not isinstance(facts.get("items"), list):
        raise ValueError("Not a DLsite fact catalogue")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dlsite-snapshot-") as temp:
        database = Path(temp) / "slgking.db"
        conn = slg_db.connect(str(database))
        try:
            ids = set()
            labels = {}
            for item in sorted(facts["items"], key=lambda value: value['external_id']):
                external = slg_dlsite.product_id(item["url"])
                slug = "dlsite-" + external.lower()
                if external != item["external_id"] or slug != item["slug"] or external in ids:
                    raise ValueError("Invalid or duplicate product identity")
                if item.get("age_rating") != "R18" or not item.get("title") or not item.get("work_type"):
                    raise ValueError("Missing eligible game evidence")
                if not slg_dlsite._GAME.search(item["work_type"]) or slg_dlsite._NON_GAME.search(item["work_type"]):
                    raise ValueError("Non-game primary type is outside the pilot scope")
                ids.add(external)
                tags = item.get("tags", [])
                if not isinstance(tags, list) or any(not tag.startswith("dlsite:genre:") for tag in tags):
                    raise ValueError("Invalid DLsite tag namespace")
                tags = sorted(set(tags))
                gid, _ = slg_db.upsert_game(
                    conn, slug, item["url"], item["title"], developer=item.get("developer") or None,
                    last_updated=item.get("updated_date") or item.get("release_date") or None,
                    lastmod=item.get("updated_date") or item.get("release_date") or None, tags=tags)
                slg_db.set_dlsite_metadata(conn, gid, {key: item.get(key) for key in sorted(FIELDS)})
                if covers and slug in covers:
                    conn.execute('UPDATE games SET cover_file=? WHERE id=?', (covers[slug]['filename'], gid))
                slg_db.upsert_game_source(conn, "dlsite", external, gid,
                    source_url=item["url"], source_title=item["title"], developer=item.get("developer"),
                    tags=tags, source_modified=item.get("updated_date") or item.get("release_date"),
                    match_status="new")
                labels.update(item.get("tag_labels", {}))
            # Export timestamps describe facts, never the wall clock of assembly.
            conn.execute("UPDATE games SET first_seen='1970-01-01T00:00:00Z', last_synced='1970-01-01T00:00:00Z'")
            conn.execute("UPDATE game_sources SET fetched_at='1970-01-01T00:00:00Z'")
            conn.execute("DELETE FROM prefs")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("VACUUM")
        finally:
            conn.close()
        raw = database.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # The EXE ships only factual tables, never cache responses, images or private state.
        probe = sqlite3.connect(database)
        try:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid snapshot database")
            count = probe.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        finally:
            probe.close()
        manifest = {"catalog_id": "dlsite", "count": count, "db_sha256": digest,
                    "collected_at": facts["collected_at"], "lastmod": facts["collected_at"],
                    "channel": "bundled_pilot", "provenance": facts.get("provenance", {})}
        for name, data in (("slgking.db", raw),
                           ("tag_zh.json", json.dumps(labels, ensure_ascii=False, indent=2).encode("utf-8")),
                           ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))):
            destination = output / name
            temporary = output / (name + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path)
    parser.add_argument("--output", type=Path, default=Path("assets/dlsite"))
    args = parser.parse_args()
    print(json.dumps(build(args.facts, args.output), ensure_ascii=False, indent=2))
