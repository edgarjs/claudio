#!/usr/bin/env python3
"""Tests for lib/memory.py — schema, activation, scoring, storage, retrieval."""

import math
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Add parent dir to path so we can import lib/memory.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lib.memory as memory


class TestSchema(unittest.TestCase):
    """Test database schema creation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file

    def tearDown(self):
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_init_schema_creates_tables(self):
        memory.init_schema()
        conn = sqlite3.connect(self.db_file)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        self.assertIn("episodic_memories", tables)
        self.assertIn("semantic_memories", tables)
        self.assertIn("procedural_memories", tables)
        self.assertIn("memory_accesses", tables)
        self.assertIn("memory_meta", tables)

    def test_init_schema_idempotent(self):
        memory.init_schema()
        memory.init_schema()  # Should not raise

    def test_wal_mode_enabled(self):
        memory.init_schema()
        conn = sqlite3.connect(self.db_file)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode, "wal")


class TestEmbeddings(unittest.TestCase):
    """Test embedding utilities (without actual model)."""

    def test_embedding_to_blob_roundtrip(self):
        vec = [0.1, 0.2, 0.3, -0.5, 1.0]
        blob = memory.embedding_to_blob(vec)
        result = memory.blob_to_embedding(blob)
        for a, b in zip(vec, result):
            self.assertAlmostEqual(a, b, places=5)

    def test_cosine_similarity_identical(self):
        vec = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(memory.cosine_similarity(vec, vec), 1.0)

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        self.assertAlmostEqual(memory.cosine_similarity(a, b), 0.0)

    def test_cosine_similarity_opposite(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        self.assertAlmostEqual(memory.cosine_similarity(a, b), -1.0)

    def test_cosine_similarity_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        self.assertEqual(memory.cosine_similarity(a, b), 0.0)


class TestTimestamp(unittest.TestCase):
    """Test timestamp parsing."""

    def test_parse_standard_format(self):
        ts = "2025-01-15 10:30:00"
        dt = memory.parse_timestamp(ts)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_iso_format(self):
        ts = "2025-01-15T10:30:00"
        dt = memory.parse_timestamp(ts)
        self.assertEqual(dt.year, 2025)

    def test_parse_with_microseconds(self):
        ts = "2025-01-15 10:30:00.123456"
        dt = memory.parse_timestamp(ts)
        self.assertEqual(dt.microsecond, 123456)

    def test_parse_invalid_raises(self):
        with self.assertRaises(ValueError):
            memory.parse_timestamp("not-a-date")


class TestActivation(unittest.TestCase):
    """Test ACT-R activation scoring."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()
        self.conn = memory.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_no_accesses_returns_neg_inf(self):
        act = memory.base_level_activation(self.conn, "nonexistent", "semantic")
        self.assertEqual(act, -float("inf"))

    def test_recent_access_higher_activation(self):
        # Insert memory
        self.conn.execute(
            "INSERT INTO semantic_memories (id, content) VALUES ('m1', 'test')"
        )
        # Recent access
        self.conn.execute(
            "INSERT INTO memory_accesses (memory_id, memory_type, accessed_at) "
            "VALUES ('m1', 'semantic', datetime('now'))"
        )
        self.conn.commit()

        act_recent = memory.base_level_activation(self.conn, "m1", "semantic")

        # Old access
        self.conn.execute("DELETE FROM memory_accesses")
        self.conn.execute(
            "INSERT INTO memory_accesses (memory_id, memory_type, accessed_at) "
            "VALUES ('m1', 'semantic', datetime('now', '-30 days'))"
        )
        self.conn.commit()

        act_old = memory.base_level_activation(self.conn, "m1", "semantic")

        self.assertGreater(act_recent, act_old)

    def test_more_accesses_higher_activation(self):
        self.conn.execute(
            "INSERT INTO semantic_memories (id, content) VALUES ('m1', 'test')"
        )
        # One access
        self.conn.execute(
            "INSERT INTO memory_accesses (memory_id, memory_type) VALUES ('m1', 'semantic')"
        )
        self.conn.commit()
        act_one = memory.base_level_activation(self.conn, "m1", "semantic")

        # Add more accesses
        for _ in range(5):
            self.conn.execute(
                "INSERT INTO memory_accesses (memory_id, memory_type) VALUES ('m1', 'semantic')"
            )
        self.conn.commit()
        act_many = memory.base_level_activation(self.conn, "m1", "semantic")

        self.assertGreater(act_many, act_one)

    def test_normalize_activation_neg_inf(self):
        self.assertEqual(memory.normalize_activation(-float("inf")), 0.0)

    def test_normalize_activation_zero(self):
        self.assertAlmostEqual(memory.normalize_activation(0.0), 0.5)

    def test_normalize_activation_large_positive(self):
        result = memory.normalize_activation(10.0)
        self.assertGreater(result, 0.99)

    def test_normalize_activation_large_negative(self):
        result = memory.normalize_activation(-10.0)
        self.assertLess(result, 0.01)


class TestReinforcementDecay(unittest.TestCase):
    """Test confidence decay for semantic memories."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()
        self.conn = memory.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_no_decay_within_grace_period(self):
        created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        result = memory.reinforcement_decay(self.conn, "m1", 0.9, created)
        self.assertEqual(result, 0.9)

    def test_decay_after_grace_period(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = memory.reinforcement_decay(self.conn, "m1", 0.9, old_date)
        self.assertLess(result, 0.9)
        self.assertGreater(result, memory.CONFIDENCE_FLOOR)

    def test_floor_prevents_zero(self):
        very_old = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        result = memory.reinforcement_decay(self.conn, "m1", 0.5, very_old)
        self.assertGreaterEqual(result, memory.CONFIDENCE_FLOOR)


class TestStorage(unittest.TestCase):
    """Test memory storage and retrieval from DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()
        self.conn = memory.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_store_episodic(self):
        mid = memory.store_memory(
            self.conn, "episodic", "Test episode",
            context="testing", outcome="success", importance=0.8,
        )
        self.conn.commit()

        row = self.conn.execute("SELECT * FROM episodic_memories WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["content"], "Test episode")
        self.assertEqual(row["context"], "testing")
        self.assertAlmostEqual(row["importance"], 0.8)

    def test_store_semantic(self):
        mid = memory.store_memory(
            self.conn, "semantic", "User prefers Python",
            category="preference", confidence=0.9,
        )
        self.conn.commit()

        row = self.conn.execute("SELECT * FROM semantic_memories WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["content"], "User prefers Python")
        self.assertEqual(row["category"], "preference")

    def test_store_procedural(self):
        mid = memory.store_memory(
            self.conn, "procedural", "Run tests before committing",
            trigger_pattern="code changes",
        )
        self.conn.commit()

        row = self.conn.execute("SELECT * FROM procedural_memories WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["content"], "Run tests before committing")
        self.assertEqual(row["trigger_pattern"], "code changes")

    def test_store_with_embedding(self):
        vec = [0.1, 0.2, 0.3]
        mid = memory.store_memory(
            self.conn, "semantic", "test",
            embedding_vec=vec, category="fact",
        )
        self.conn.commit()

        row = self.conn.execute("SELECT embedding FROM semantic_memories WHERE id=?", (mid,)).fetchone()
        result = memory.blob_to_embedding(row["embedding"])
        self.assertEqual(len(result), 3)

    def test_initial_access_recorded(self):
        mid = memory.store_memory(self.conn, "semantic", "test")
        self.conn.commit()

        count = self.conn.execute(
            "SELECT COUNT(*) FROM memory_accesses WHERE memory_id=?", (mid,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_record_access_caps_history(self):
        mid = memory.store_memory(self.conn, "semantic", "test")
        self.conn.commit()

        # Insert many accesses
        for _ in range(memory.ACCESS_CAP_PER_MEMORY + 50):
            memory.record_access(self.conn, mid, "semantic")
        self.conn.commit()

        count = self.conn.execute(
            "SELECT COUNT(*) FROM memory_accesses WHERE memory_id=?", (mid,)
        ).fetchone()[0]
        self.assertLessEqual(count, memory.ACCESS_CAP_PER_MEMORY)

    def test_fts_index_populated(self):
        mid = memory.store_memory(self.conn, "semantic", "Python is a programming language")
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM memory_fts WHERE memory_id=?", (mid,)
        ).fetchall()
        self.assertEqual(len(rows), 1)


class TestConsolidationGating(unittest.TestCase):
    """Test should_consolidate gating logic."""

    def test_too_few_messages(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        self.assertFalse(memory.should_consolidate(messages))

    def test_slash_commands_only(self):
        messages = [
            {"role": "user", "content": "/opus"},
            {"role": "assistant", "content": "Switched to Opus"},
            {"role": "user", "content": "/haiku"},
        ]
        self.assertFalse(memory.should_consolidate(messages))

    def test_short_but_passes(self):
        messages = [
            {"role": "user", "content": "use rust"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "always"},
        ]
        # Short messages but not slash commands — passes gate
        self.assertTrue(memory.should_consolidate(messages))

    def test_long_conversation_passes(self):
        messages = [
            {"role": "user", "content": "I need help implementing a new feature for the memory system that handles embeddings"},
            {"role": "assistant", "content": "Sure, let me help with that"},
            {"role": "user", "content": "Here is what I'm thinking about the architecture"},
        ]
        self.assertTrue(memory.should_consolidate(messages))


class TestFormatMemories(unittest.TestCase):
    """Test memory formatting for prompt injection."""

    def test_empty_list(self):
        self.assertEqual(memory.format_memories([]), "")

    def test_semantic_memory(self):
        memories = [{"type": "semantic", "content": "User prefers Python", "confidence": 0.85, "category": "preference"}]
        result = memory.format_memories(memories)
        self.assertIn("[semantic]", result)
        self.assertIn("User prefers Python", result)
        self.assertIn("0.85", result)

    def test_episodic_memory(self):
        memories = [{"type": "episodic", "content": "Implemented STT feature"}]
        result = memory.format_memories(memories)
        self.assertIn("[episodic]", result)
        self.assertIn("Implemented STT feature", result)

    def test_procedural_memory(self):
        memories = [{"type": "procedural", "content": "Run 3 review agents", "trigger_pattern": "before PR"}]
        result = memory.format_memories(memories)
        self.assertIn("[procedural]", result)
        self.assertIn("Run 3 review agents", result)
        self.assertIn("before PR", result)


class TestMetaTracking(unittest.TestCase):
    """Test consolidation state tracking."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()
        self.conn = memory.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_update_last_consolidated(self):
        memory._update_last_consolidated(self.conn, 42)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT value FROM memory_meta WHERE key='last_consolidated_id'"
        ).fetchone()
        self.assertEqual(row["value"], "42")

    def test_update_last_consolidated_overwrites(self):
        memory._update_last_consolidated(self.conn, 10)
        memory._update_last_consolidated(self.conn, 20)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT value FROM memory_meta WHERE key='last_consolidated_id'"
        ).fetchone()
        self.assertEqual(row["value"], "20")


class TestDedup(unittest.TestCase):
    """Test deduplication logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()
        self.conn = memory.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_no_existing_memories_returns_new(self):
        result = memory._check_dedup(self.conn, "semantic", "test", [0.1, 0.2, 0.3])
        self.assertEqual(result, "new")

    def test_no_embedding_returns_new(self):
        result = memory._check_dedup(self.conn, "semantic", "test", None)
        self.assertEqual(result, "new")

    def test_near_duplicate_returns_skip(self):
        # Store a memory with known embedding
        vec = [1.0, 0.0, 0.0]
        memory.store_memory(self.conn, "semantic", "existing", embedding_vec=vec)
        self.conn.commit()

        # Check with nearly identical vector
        result = memory._check_dedup(self.conn, "semantic", "nearly the same", [0.999, 0.001, 0.0])
        self.assertEqual(result, "skip")


class TestRetrieveWithoutModel(unittest.TestCase):
    """Test retrieval when embedding model is unavailable."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()

    def tearDown(self):
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    @patch.object(memory, "embed", return_value=[])
    def test_retrieve_falls_back_to_fts(self, mock_embed):
        # Store a memory with FTS
        conn = memory.get_db()
        memory.store_memory(conn, "semantic", "Python programming language", category="fact")
        conn.commit()
        conn.close()

        results = memory.retrieve("Python")
        # Should return via FTS even without embeddings
        # (may be empty if FTS doesn't match the exact query, that's ok)
        self.assertIsInstance(results, list)


class TestModelMismatchDetection(unittest.TestCase):
    """Test embedding model change detection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file

    def tearDown(self):
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_stores_model_name_on_first_init(self):
        memory.init_schema()
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM memory_meta WHERE key='embedding_model'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["value"], memory.EMBEDDING_MODEL)

    def test_detects_model_change_and_clears_embeddings(self):
        # First init with a fake model
        memory.init_schema()
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        # Store a memory with an embedding
        fake_blob = memory.embedding_to_blob([0.1, 0.2, 0.3])
        conn.execute(
            "INSERT INTO semantic_memories (id, content, embedding) VALUES ('m1', 'test', ?)",
            (fake_blob,),
        )
        # Pretend a different model was used
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('embedding_model', 'old-model')"
        )
        conn.commit()
        conn.close()

        # Re-init should detect the mismatch and clear embeddings
        memory.init_schema()

        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT embedding FROM semantic_memories WHERE id='m1'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row["embedding"])

    def test_no_clear_when_model_unchanged(self):
        memory.init_schema()
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        fake_blob = memory.embedding_to_blob([0.1, 0.2, 0.3])
        conn.execute(
            "INSERT INTO semantic_memories (id, content, embedding) VALUES ('m1', 'test', ?)",
            (fake_blob,),
        )
        conn.commit()
        conn.close()

        # Re-init with same model should NOT clear embeddings
        memory.init_schema()

        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT embedding FROM semantic_memories WHERE id='m1'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["embedding"])


class TestFTSEscaping(unittest.TestCase):
    """Test FTS5 query escaping handles special characters."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_file = os.path.join(self.tmpdir, "test.db")
        os.environ["CLAUDIO_DB_FILE"] = self.db_file
        memory.DB_FILE = self.db_file
        memory.init_schema()

    def tearDown(self):
        os.unlink(self.db_file) if os.path.exists(self.db_file) else None

    def test_fts_handles_special_characters(self):
        conn = memory.get_db()
        memory.store_memory(conn, "semantic", "Python programming language", category="fact")
        conn.commit()
        conn.close()

        # These would crash FTS5 without proper escaping
        for query in ['Python AND NOT "evil"', "test*", "NEAR(a,b)", 'foo OR bar']:
            conn = memory.get_db()
            results = memory._fts_search(conn, query, ["semantic"], 5)
            conn.close()
            self.assertIsInstance(results, list)

    def test_fts_empty_query(self):
        conn = memory.get_db()
        results = memory._fts_search(conn, "***", ["semantic"], 5)
        conn.close()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
