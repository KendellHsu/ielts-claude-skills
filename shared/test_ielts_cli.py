#!/usr/bin/env python3
"""Tests for ielts_cli.py — stdlib unittest only.

Every test runs the CLI as a subprocess with IELTS_HOME pointed at a fresh
temporary directory, and uses a temporary fake vault. The real ~/.ielts and the
real Obsidian vault are never touched.

Run:  python3 shared/test_ielts_cli.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent / "ielts_cli.py"


class CLITestCase(unittest.TestCase):
    """Base: isolated IELTS_HOME + isolated fake vault."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ielts-test-")
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "ielts-home"
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True)
        self.root = self.vault / "IELTS"
        # Safety net: make sure we are nowhere near the real data.
        self.assertNotIn(str(Path.home() / ".ielts"), str(self.home))
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *args, expect_code=0):
        env = dict(os.environ)
        env["IELTS_HOME"] = str(self.home)
        proc = subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True, text=True, env=env,
        )
        if expect_code is not None:
            self.assertEqual(
                proc.returncode, expect_code,
                f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )
        return proc

    def json_cli(self, *args, expect_code=0):
        proc = self.run_cli(*args, expect_code=expect_code)
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"non-JSON output for {args}:\n{proc.stdout}\n{proc.stderr}")

    def init(self):
        self.json_cli("init")

    def use_vault(self):
        self.json_cli("config", "set", "--vault-path", str(self.vault))

    def seed(self):
        """A small but representative data set."""
        self.init()
        self.json_cli("config", "set", "--target-score", "7.5", "--exam-date", "2026-12-01")
        self.json_cli(
            "writing", "add",
            "--task-type", "Task 2",
            "--topic", "Online education",
            "--word-count", "268",
            "--scores", '{"TR": 6, "CC": 5.5, "LR": 6, "GRA": 6}',
            "--key-issues", '["missing overview", "weak cohesion"]',
            "--content", "Some people believe online education is better.\n\nIn conclusion, it is not.",
        )
        self.json_cli(
            "vocab", "add",
            "--word", "ubiquitous",
            "--definition", "present everywhere",
            "--example", "Smartphones are ubiquitous.",
            "--synonyms", '["omnipresent", "pervasive"]',
            "--source", "C18 Test 2",
        )
        self.json_cli("memory", "add", "--content", "Loses focus in Section 4",
                      "--category", "weakness", "--skill", "listening", "--priority", "high")
        self.json_cli("speaking", "add", "--topic", "A book you enjoyed",
                      "--part", "Part 2", "--group", "story-A", "--notes", "Used the travel story.")
        self.json_cli("reading", "add", "--passage-title", "Bee colonies",
                      "--total-questions", "13", "--correct", "9", "--score", "6.5",
                      "--key-errors", '["scanning too slow"]')
        self.json_cli("listening", "add", "--test-name", "C17 T1",
                      "--total-questions", "40", "--correct", "30", "--score", "7.0")

    @staticmethod
    def frontmatter(path: Path) -> dict:
        """Minimal frontmatter reader for assertions."""
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"no frontmatter in {path}"
        lines = text.split("\n")
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        out, key = {}, None
        for line in lines[1:end]:
            if line.startswith((" ", "\t")) or line.startswith("-"):
                if key is not None and line.strip().startswith("- "):
                    out.setdefault(key + "__list", []).append(line.strip()[2:].strip())
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                out[key] = val.strip().strip('"')
        return out


class TestExistingCommands(CLITestCase):
    """Regression: nothing that worked before may break."""

    def test_init_creates_layout(self):
        out = self.json_cli("init")
        self.assertEqual(out["status"], "ok")
        for name in ["config.json", "errors.json", "synonyms.json", "progress.json",
                     "vocab.json", "memories.json"]:
            self.assertTrue((self.home / name).exists(), name)
        for d in ["writing", "reading", "listening", "speaking"]:
            self.assertTrue((self.home / d / "index.json").exists(), d)

    def test_init_is_idempotent(self):
        self.init()
        self.json_cli("config", "set", "--target-score", "7.0")
        self.json_cli("init")
        self.assertEqual(self.json_cli("config", "get")["target_score"], 7.0)

    def test_config_has_vault_path_default(self):
        self.init()
        cfg = self.json_cli("config", "get")
        self.assertEqual(cfg["vault_path"], "")

    def test_writing_vocab_memory_progress(self):
        self.seed()
        essays = self.json_cli("writing", "list")
        self.assertEqual(len(essays), 1)
        self.assertEqual(essays[0]["topic"], "Online education")
        self.assertAlmostEqual(essays[0]["total"], 6.0)

        vocab = self.json_cli("vocab", "list")
        self.assertEqual(vocab[0]["word"], "ubiquitous")
        self.assertEqual(vocab[0]["ease_factor"], 2.5)

        mems = self.json_cli("memory", "list")
        self.assertEqual(mems[0]["category"], "weakness")

        prog = self.json_cli("progress", "show")
        self.assertEqual(prog["target"], 7.5)
        self.assertEqual(prog["writing_latest"], 6.0)
        self.assertEqual(prog["reading_latest"], 6.5)
        self.assertEqual(prog["vocab_count"], 1)

    def test_status_and_errors(self):
        self.seed()
        proc = self.run_cli("status")
        self.assertIn("IELTS", proc.stdout)
        errors = self.json_cli("error", "list", "--category", "writing")
        tags = {e["tag"] for e in errors}
        self.assertIn("missing overview", tags)

    def test_vocab_sm2_update(self):
        self.seed()
        out = self.json_cli("vocab", "update", "--word", "ubiquitous", "--quality", "5")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["word"]["repetitions"], 1)
        self.assertEqual(out["word"]["interval"], 1)


class TestFailSoft(CLITestCase):
    def test_export_skipped_when_vault_unset(self):
        self.init()
        out = self.json_cli("vault", "export", expect_code=0)
        self.assertEqual(out["status"], "skipped")
        self.assertIn("not configured", out["reason"])

    def test_export_skipped_when_path_missing(self):
        self.init()
        missing = self.tmp / "nas" / "not-mounted"
        self.json_cli("config", "set", "--vault-path", str(missing))
        out = self.json_cli("vault", "export", expect_code=0)
        self.assertEqual(out["status"], "skipped")
        self.assertIn("does not exist", out["reason"])
        self.assertFalse(missing.exists(), "must not create the missing vault path")

    def test_import_skipped_when_path_missing(self):
        self.init()
        self.json_cli("config", "set", "--vault-path", str(self.tmp / "nope"))
        out = self.json_cli("vault", "import", expect_code=0)
        self.assertEqual(out["status"], "skipped")

    def test_status_skipped_when_unavailable(self):
        self.init()
        out = self.json_cli("vault", "status", expect_code=0)
        self.assertEqual(out["status"], "skipped")
        self.assertFalse(out["available"])

    def test_export_skipped_when_not_writable(self):
        self.init()
        locked = self.tmp / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        self.json_cli("config", "set", "--vault-path", str(locked))
        out = self.json_cli("vault", "export", expect_code=0)
        self.assertEqual(out["status"], "skipped")
        self.assertIn("not writable", out["reason"])

    def test_export_skipped_when_path_is_a_file(self):
        self.init()
        f = self.tmp / "afile.md"
        f.write_text("hi", encoding="utf-8")
        self.json_cli("config", "set", "--vault-path", str(f))
        out = self.json_cli("vault", "export", expect_code=0)
        self.assertEqual(out["status"], "skipped")


class TestExport(CLITestCase):
    def test_creates_expected_files(self):
        self.seed()
        self.use_vault()
        out = self.json_cli("vault", "export")
        self.assertEqual(out["status"], "ok")
        self.assertGreater(out["totals"]["created"], 0)

        self.assertTrue((self.root / "IELTS.md").exists())
        self.assertTrue((self.root / "進度.md").exists())
        self.assertTrue((self.root / "錯題本.md").exists())
        self.assertTrue((self.root / "vocab" / "ubiquitous.md").exists())
        self.assertTrue((self.root / "synonym" / "ubiquitous.md").exists())
        self.assertEqual(len(list((self.root / "writing").glob("*.md"))), 1)
        self.assertEqual(len(list((self.root / "memory").glob("*.md"))), 1)
        self.assertEqual(len(list((self.root / "speaking").glob("*.md"))), 1)
        # reading / listening must NOT produce per-record notes
        self.assertFalse((self.root / "reading").exists())
        self.assertFalse((self.root / "listening").exists())

    def test_vocab_frontmatter_and_links(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = self.root / "vocab" / "ubiquitous.md"
        fm = self.frontmatter(note)
        self.assertEqual(fm["ielts_type"], "vocab")
        self.assertEqual(fm["ielts_managed"], "true")
        self.assertEqual(fm["ielts_id"], "ubiquitous")
        self.assertEqual(fm["word"], "ubiquitous")
        self.assertEqual(fm["ease_factor"], "2.5")
        self.assertEqual(fm["next_review"], fm["date_added"])
        text = note.read_text(encoding="utf-8")
        self.assertIn("<!-- ielts:generated:start -->", text)
        self.assertIn("<!-- ielts:generated:end -->", text)
        self.assertIn("[[omnipresent]]", text)
        self.assertIn("[[pervasive]]", text)

    def test_writing_note_contains_essay_and_scores(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = next((self.root / "writing").glob("*.md"))
        fm = self.frontmatter(note)
        self.assertEqual(fm["ielts_type"], "writing")
        self.assertEqual(fm["topic"], "Online education")
        self.assertEqual(fm["word_count"], "268")
        text = note.read_text(encoding="utf-8")
        self.assertIn("TR: 6", text)          # nested scores mapping
        self.assertIn("- missing overview", text)  # key_issues list
        self.assertIn("Some people believe online education is better.", text)
        self.assertNotIn("**Task Type:**", text)   # archive header stripped

    def test_memory_note_fields(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = next((self.root / "memory").glob("*.md"))
        fm = self.frontmatter(note)
        self.assertEqual(fm["ielts_type"], "memory")
        self.assertEqual(fm["category"], "weakness")
        self.assertEqual(fm["skill"], "listening")
        self.assertEqual(fm["priority"], "high")
        self.assertIn("ielts/memory/weakness", fm.get("tags__list", []))
        self.assertIn("#ielts/memory/weakness", note.read_text(encoding="utf-8"))

    def test_progress_note_has_reading_and_listening_tables(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        text = (self.root / "進度.md").read_text(encoding="utf-8")
        self.assertIn("## 閱讀記錄", text)
        self.assertIn("Bee colonies", text)
        self.assertIn("## 聽力記錄", text)
        self.assertIn("C17 T1", text)
        self.assertIn("| 目標分 | 7.5 |", text)

    def test_errors_note_sorted_by_count(self):
        self.init()
        self.use_vault()
        for _ in range(3):
            self.json_cli("error", "add", "--category", "writing", "--tag", "frequent")
        self.json_cli("error", "add", "--category", "writing", "--tag", "rare")
        self.json_cli("vault", "export", "--only", "errors")
        text = (self.root / "錯題本.md").read_text(encoding="utf-8")
        self.assertLess(text.index("frequent"), text.index("rare"))

    def test_moc_links(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        text = (self.root / "IELTS.md").read_text(encoding="utf-8")
        self.assertIn("[[IELTS/進度|進度]]", text)
        self.assertIn("[[IELTS/錯題本|錯題本]]", text)
        self.assertIn("[[ubiquitous]]", text)

    def test_only_filter(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export", "--only", "vocab")
        self.assertTrue((self.root / "vocab").exists())
        self.assertFalse((self.root / "writing").exists())
        self.assertFalse((self.root / "進度.md").exists())

    def test_dry_run_writes_nothing(self):
        self.seed()
        self.use_vault()
        out = self.json_cli("vault", "export", "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertGreater(out["totals"]["created"], 0)
        self.assertFalse(self.root.exists(), "dry-run must not create the vault root")

    def test_second_export_rewrites_nothing(self):
        self.seed()
        self.use_vault()
        first = self.json_cli("vault", "export")
        self.assertEqual(first["totals"]["unchanged"], 0)

        note = self.root / "vocab" / "ubiquitous.md"
        before_mtime = note.stat().st_mtime_ns
        before_all = {p: p.stat().st_mtime_ns for p in self.root.rglob("*.md")}

        second = self.json_cli("vault", "export")
        self.assertEqual(second["totals"]["created"], 0)
        self.assertEqual(second["totals"]["updated"], 0)
        self.assertEqual(second["totals"]["unchanged"], first["totals"]["created"])
        self.assertEqual(note.stat().st_mtime_ns, before_mtime)
        for p, mt in before_all.items():
            self.assertEqual(p.stat().st_mtime_ns, mt, f"{p.name} was rewritten")

    def test_data_change_updates_only_that_note(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self.json_cli("vocab", "update", "--word", "ubiquitous", "--quality", "5")
        out = self.json_cli("vault", "export", "--only", "vocab")
        self.assertEqual(out["by_type"]["vocab"]["updated"], 1)
        self.assertEqual(out["by_type"]["vocab"]["created"], 0)
        # the MOC always regenerates; its "due today" count legitimately changed
        self.assertEqual(out["by_type"]["moc"]["updated"], 1)

    def test_unrelated_notes_not_touched_by_data_change(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        essay = next((self.root / "writing").glob("*.md"))
        before = essay.stat().st_mtime_ns
        self.json_cli("vocab", "update", "--word", "ubiquitous", "--quality", "5")
        out = self.json_cli("vault", "export")
        self.assertEqual(out["by_type"]["writing"]["unchanged"], 1)
        self.assertEqual(essay.stat().st_mtime_ns, before)


class TestConflictPreservation(CLITestCase):
    def test_handwritten_content_and_custom_keys_survive(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = self.root / "vocab" / "ubiquitous.md"

        text = note.read_text(encoding="utf-8")
        # user adds a frontmatter key and hand-written body text below the block
        text = text.replace("---\nword: ubiquitous", "---\nmy_rating: ⭐⭐⭐\nword: ubiquitous", 1)
        text = text.rstrip("\n") + "\n\n## 我的筆記\n這個字在 C18 出現過兩次。\n"
        note.write_text(text, encoding="utf-8")

        # change the underlying data so the generated block must be rewritten
        self.json_cli("vocab", "update", "--word", "ubiquitous", "--quality", "4")
        out = self.json_cli("vault", "export", "--only", "vocab")
        self.assertEqual(out["by_type"]["vocab"]["updated"], 1)

        after = note.read_text(encoding="utf-8")
        self.assertIn("my_rating: ⭐⭐⭐", after)
        self.assertIn("## 我的筆記", after)
        self.assertIn("這個字在 C18 出現過兩次。", after)
        self.assertEqual(self.frontmatter(note)["ielts_managed"], "true")
        self.assertEqual(self.frontmatter(note)["repetitions"], "1")
        # hand-written text sits outside the generated block
        self.assertGreater(after.index("## 我的筆記"), after.index("<!-- ielts:generated:end -->"))

    def test_generated_block_content_is_replaced_not_duplicated(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = self.root / "vocab" / "ubiquitous.md"
        self.json_cli("vocab", "update", "--word", "ubiquitous", "--quality", "5")
        self.json_cli("vault", "export", "--only", "vocab")
        text = note.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- ielts:generated:start -->"), 1)
        self.assertEqual(text.count("<!-- ielts:generated:end -->"), 1)
        self.assertEqual(text.count("## Synonyms"), 1)

    def test_user_edits_inside_block_are_overwritten(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = self.root / "vocab" / "ubiquitous.md"
        text = note.read_text(encoding="utf-8").replace("present everywhere", "GARBAGE EDIT")
        note.write_text(text, encoding="utf-8")
        out = self.json_cli("vault", "export", "--only", "vocab")
        self.assertEqual(out["totals"]["updated"], 1)
        after = note.read_text(encoding="utf-8")
        self.assertNotIn("GARBAGE EDIT", after)
        self.assertIn("present everywhere", after)


class TestImport(CLITestCase):
    def _write_manual_vocab(self, filename, body):
        d = self.root / "vocab"
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text(body, encoding="utf-8")

    def test_import_manual_vocab_note(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab(
            "meticulous.md",
            "---\nword: meticulous\ndefinition: very careful about details\n"
            "example: A meticulous researcher.\n---\n\n手寫的備註。\n",
        )

        out = self.json_cli("vault", "import")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["imported"]["vocab"], 1)

        vocab = {v["word"]: v for v in self.json_cli("vocab", "list")}
        self.assertIn("meticulous", vocab)
        self.assertEqual(vocab["meticulous"]["definition"], "very careful about details")
        self.assertEqual(vocab["meticulous"]["ease_factor"], 2.5)
        self.assertEqual(vocab["meticulous"]["next_review"], vocab["meticulous"]["date_added"])

        note = self.root / "vocab" / "meticulous.md"
        fm = self.frontmatter(note)
        self.assertEqual(fm["ielts_managed"], "true")
        self.assertEqual(fm["ielts_id"], "meticulous")
        self.assertIn("手寫的備註。", note.read_text(encoding="utf-8"))

    def test_import_is_not_repeated(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab("meticulous.md", "---\nword: meticulous\n---\n\nvery careful\n")
        self.json_cli("vault", "import")
        second = self.json_cli("vault", "import")
        self.assertEqual(second["imported"]["vocab"], 0)
        self.assertEqual(len(self.json_cli("vocab", "list")), 2)

    def test_import_renames_to_canonical_path(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab("我的新單字.md", "---\nword: eloquent\ndefinition: fluent\n---\n\n備註\n")
        out = self.json_cli("vault", "import")
        self.assertEqual(out["imported"]["vocab"], 1)
        self.assertTrue((self.root / "vocab" / "eloquent.md").exists())
        self.assertFalse((self.root / "vocab" / "我的新單字.md").exists())
        third = self.json_cli("vault", "import")
        self.assertEqual(third["imported"]["vocab"], 0)

    def test_import_uses_filename_when_no_word_key(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab("resilient.md", "able to recover quickly\n")
        out = self.json_cli("vault", "import")
        self.assertEqual(out["imported"]["vocab"], 1)
        v = {x["word"]: x for x in self.json_cli("vocab", "list")}["resilient"]
        self.assertEqual(v["definition"], "able to recover quickly")

    def test_managed_notes_are_never_read_back(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        note = self.root / "vocab" / "ubiquitous.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("ease_factor: 2.5", "ease_factor: 9.9"),
            encoding="utf-8",
        )
        out = self.json_cli("vault", "import")
        self.assertEqual(out["imported"]["vocab"], 0)
        self.assertEqual(self.json_cli("vocab", "list")[0]["ease_factor"], 2.5)

    def test_import_manual_memory_note(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        d = self.root / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "隨手記.md").write_text(
            "---\ncategory: preference\nskill: writing\npriority: high\n---\n\n"
            "偏好晚上寫作文，早上做閱讀。\n",
            encoding="utf-8",
        )
        out = self.json_cli("vault", "import")
        self.assertEqual(out["imported"]["memory"], 1)
        mems = self.json_cli("memory", "list")
        self.assertEqual(len(mems), 2)
        added = [m for m in mems if m["category"] == "preference"][0]
        self.assertEqual(added["skill"], "writing")
        self.assertEqual(added["priority"], "high")
        self.assertIn("偏好晚上寫作文", added["content"])
        self.assertEqual(self.json_cli("vault", "import")["imported"]["memory"], 0)

    def test_import_dry_run_changes_nothing(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab("meticulous.md", "---\nword: meticulous\n---\n\ncareful\n")
        out = self.json_cli("vault", "import", "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["imported"]["vocab"], 1)
        self.assertEqual(len(self.json_cli("vocab", "list")), 1)
        self.assertNotIn("ielts_id", self.frontmatter(self.root / "vocab" / "meticulous.md"))

    def test_import_only_filter(self):
        self.seed()
        self.use_vault()
        self.json_cli("vault", "export")
        self._write_manual_vocab("meticulous.md", "---\nword: meticulous\n---\n\ncareful\n")
        out = self.json_cli("vault", "import", "--only", "memory")
        self.assertEqual(out["imported"]["vocab"], 0)
        self.assertEqual(len(self.json_cli("vocab", "list")), 1)


class TestVaultStatus(CLITestCase):
    def test_status_reports_counts(self):
        self.seed()
        self.use_vault()
        before = self.json_cli("vault", "status")
        self.assertTrue(before["available"])
        self.assertEqual(before["types"]["vocab"]["source"], 1)
        self.assertEqual(before["types"]["vocab"]["notes"], 0)

        self.json_cli("vault", "export")
        after = self.json_cli("vault", "status")
        self.assertEqual(after["types"]["vocab"]["notes"], 1)
        self.assertEqual(after["types"]["writing"]["notes"], 1)
        self.assertEqual(after["types"]["memory"]["notes"], 1)
        self.assertEqual(after["unmanaged_notes"]["vocab"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
