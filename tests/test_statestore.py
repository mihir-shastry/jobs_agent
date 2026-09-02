"""Tests for the file-based seen store."""

from datetime import datetime, timedelta, timezone

from jobagent.statestore import SeenStore


class TestSeenStore:
    def test_missing_file_starts_empty(self, tmp_path):
        store = SeenStore(tmp_path / "seen.jsonl")
        store.load()
        assert len(store) == 0
        assert "greenhouse:acme:1" not in store

    def test_add_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "seen.jsonl"
        store = SeenStore(path)
        store.add("greenhouse:acme:1")
        store.add("lever:beta:abc")
        store.save()

        reloaded = SeenStore(path)
        reloaded.load()
        assert len(reloaded) == 2
        assert "greenhouse:acme:1" in reloaded
        assert "lever:beta:abc" in reloaded

    def test_prune_older_than_retention(self, tmp_path):
        path = tmp_path / "seen.jsonl"
        store = SeenStore(path, retention_days=30)
        now = datetime.now(timezone.utc)
        store.add("fresh", now)
        store.add("stale", now - timedelta(days=45))
        store.save()

        reloaded = SeenStore(path)
        reloaded.load()
        assert "fresh" in reloaded
        assert "stale" not in reloaded

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "seen.jsonl"
        path.write_text(
            '{"k": "good:1", "t": 1700000000.0}\n'
            "not json at all\n"
            '{"no_key_here": true}\n'
            '\n',
            encoding="utf-8",
        )
        store = SeenStore(path)
        store.load()
        assert len(store) == 1
        assert "good:1" in store

    def test_save_is_atomic_and_sorted(self, tmp_path):
        path = tmp_path / "seen.jsonl"
        store = SeenStore(path)
        now = datetime.now(timezone.utc)
        store.add("b", now - timedelta(days=1))
        store.add("a", now - timedelta(days=2))
        store.save()
        lines = path.read_text().strip().splitlines()
        keys = [line.split('"k":"')[1].split('"')[0] for line in lines]
        assert keys == ["a", "b"]
        assert not path.with_suffix(".jsonl.tmp").exists()
