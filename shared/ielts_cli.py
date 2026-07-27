#!/usr/bin/env python3
"""IELTS Claude Skills — Data Layer CLI.

A single-file, stdlib-only Python script that manages all data persistence
for the IELTS Claude Skills system. Called by SKILL.md prompts via Bash.

Usage:
  python3 ielts_cli.py init
  python3 ielts_cli.py config get
  python3 ielts_cli.py config set --target 7.0 --exam-date 2026-06-15
  python3 ielts_cli.py writing add --task-type "Task 2" --topic "..." --scores '{"TR":6,"CC":5.5,"LR":5.5,"GRA":6}' --content "..."
  python3 ielts_cli.py writing list [--last 5]
  ...
"""

import argparse
import json
import os
import re
import sys
import shutil
import zipfile
import hashlib
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────
# All data lives under IELTS_DIR. Default is ~/.ielts, overridable with the
# IELTS_HOME environment variable (used by the test suite so it never touches
# the real user data). The module-level constants below are recomputed by
# _resolve_paths(), which runs at import time and again at the top of main().

IELTS_DIR = None
CONFIG_FILE = None
ERRORS_FILE = None
SYNONYMS_FILE = None
PROGRESS_FILE = None
VOCAB_FILE = None
WRITING_DIR = None
READING_DIR = None
LISTENING_DIR = None
SPEAKING_DIR = None
MEMORY_FILE = None
DASHBOARD_FILE = None
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard" / "template.html"


def _ielts_home() -> Path:
    """Resolve the data root: $IELTS_HOME if set and non-empty, else ~/.ielts."""
    env = os.environ.get("IELTS_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ielts"


def _resolve_paths():
    """(Re)compute every module-level path constant from the current environment."""
    global IELTS_DIR, CONFIG_FILE, ERRORS_FILE, SYNONYMS_FILE, PROGRESS_FILE
    global VOCAB_FILE, WRITING_DIR, READING_DIR, LISTENING_DIR, SPEAKING_DIR
    global MEMORY_FILE, DASHBOARD_FILE

    IELTS_DIR = _ielts_home()
    CONFIG_FILE = IELTS_DIR / "config.json"
    ERRORS_FILE = IELTS_DIR / "errors.json"
    SYNONYMS_FILE = IELTS_DIR / "synonyms.json"
    PROGRESS_FILE = IELTS_DIR / "progress.json"
    VOCAB_FILE = IELTS_DIR / "vocab.json"
    WRITING_DIR = IELTS_DIR / "writing"
    READING_DIR = IELTS_DIR / "reading"
    LISTENING_DIR = IELTS_DIR / "listening"
    SPEAKING_DIR = IELTS_DIR / "speaking"
    MEMORY_FILE = IELTS_DIR / "memories.json"
    DASHBOARD_FILE = IELTS_DIR / "dashboard.html"


_resolve_paths()


# ── Data Models ────────────────────────────────────────────────────

@dataclass
class Config:
    target_score: float = 0.0
    exam_date: str = ""  # YYYY-MM-DD
    listening: float = 0.0
    reading: float = 0.0
    writing: float = 0.0
    speaking: float = 0.0
    name: str = ""
    updated_at: str = ""
    vault_path: str = ""  # Obsidian vault root (may live on a NAS mount)

    def days_until_exam(self) -> int:
        if not self.exam_date:
            return 0
        try:
            d = datetime.strptime(self.exam_date, "%Y-%m-%d").date()
            return (d - date.today()).days
        except ValueError:
            return 0

    def overall_band(self) -> float:
        scores = [s for s in [self.listening, self.reading, self.writing, self.speaking] if s > 0]
        if not scores:
            return 0.0
        avg = sum(scores) / len(scores)
        return _round_ielts(avg)


@dataclass
class EssayRecord:
    date: str
    task_type: str
    topic: str
    word_count: int
    scores: dict  # {TR, CC, LR, GRA}
    total: float
    key_issues: list = field(default_factory=list)
    file: str = ""


@dataclass
class ReadingRecord:
    date: str
    passage_title: str
    total_questions: int
    correct: int
    score: float
    question_types: dict = field(default_factory=dict)  # {type: {total, correct}}
    synonyms_added: int = 0
    key_errors: list = field(default_factory=list)


@dataclass
class ListeningRecord:
    date: str
    test_name: str
    total_questions: int
    correct: int
    score: float
    section_scores: dict = field(default_factory=dict)
    question_type_errors: dict = field(default_factory=dict)
    key_errors: list = field(default_factory=list)


@dataclass
class SpeakingRecord:
    date: str
    topic: str
    part: str  # Part 1 / Part 2 / Part 3
    group: str = ""  # which universal story group
    notes: str = ""


@dataclass
class VocabWord:
    word: str
    definition: str
    example: str
    synonyms: list = field(default_factory=list)
    source: str = ""  # where encountered
    date_added: str = ""
    ease_factor: float = 2.5
    interval: int = 0
    repetitions: int = 0
    next_review: str = ""  # YYYY-MM-DD
    last_reviewed: str = ""


@dataclass
class CoachMemory:
    id: str
    date: str
    category: str  # observation, preference, weakness, strength, strategy, note
    skill: str     # general, writing, reading, listening, speaking, vocab
    content: str
    source: str = ""
    priority: str = "medium"


# ── Utilities ──────────────────────────────────────────────────────

def _round_ielts(score: float) -> float:
    """Round to nearest 0.5, with .25/.75 rounding up."""
    whole = int(score)
    frac = score - whole
    if frac < 0.25:
        return float(whole)
    elif frac < 0.75:
        return whole + 0.5
    else:
        return float(whole + 1)


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default


def _save_json(path: Path, data):
    _ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _today() -> str:
    return date.today().isoformat()


def _slug(text: str, max_len: int = 40) -> str:
    """Create a safe filename slug from text."""
    slug = "".join(c if c.isalnum() or c in " -_" else "" for c in text.lower())
    slug = slug.strip().replace(" ", "-")[:max_len]
    return slug or "untitled"


# ── Commands ───────────────────────────────────────────────────────

def cmd_init():
    """Initialize ~/.ielts/ directory and default config."""
    _ensure_dir(IELTS_DIR)
    _ensure_dir(WRITING_DIR)
    _ensure_dir(READING_DIR)
    _ensure_dir(LISTENING_DIR)
    _ensure_dir(SPEAKING_DIR)

    if not CONFIG_FILE.exists():
        config = Config(updated_at=_today())
        _save_json(CONFIG_FILE, asdict(config))

    for f, default in [
        (ERRORS_FILE, {"writing": [], "reading": [], "listening": [], "speaking": []}),
        (SYNONYMS_FILE, []),
        (PROGRESS_FILE, {"writing_scores": [], "reading_scores": [], "listening_scores": [], "speaking_scores": []}),
        (VOCAB_FILE, []),
        (MEMORY_FILE, []),
    ]:
        if not f.exists():
            _save_json(f, default)

    for d, default in [
        (WRITING_DIR / "index.json", []),
        (READING_DIR / "index.json", []),
        (LISTENING_DIR / "index.json", []),
        (SPEAKING_DIR / "index.json", []),
    ]:
        if not d.exists():
            _save_json(d, default)

    print(json.dumps({"status": "ok", "message": "IELTS data directory initialized", "path": str(IELTS_DIR)}))
    return 0


def cmd_config_get():
    """Read and output config.json."""
    config = _load_json(CONFIG_FILE, asdict(Config()))
    cfg = Config(**{k: v for k, v in config.items() if k in Config.__dataclass_fields__})
    config["days_until_exam"] = cfg.days_until_exam()
    config["overall_band"] = cfg.overall_band()
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


def cmd_config_set(args):
    """Update config fields."""
    config = _load_json(CONFIG_FILE, asdict(Config()))

    field_map = {
        "target_score": "target_score",
        "exam_date": "exam_date",
        "listening": "listening",
        "reading": "reading",
        "writing": "writing",
        "speaking": "speaking",
        "name": "name",
        "vault_path": "vault_path",
    }

    updated = False
    for arg_name, config_key in field_map.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            if config_key in ("target_score", "listening", "reading", "writing", "speaking"):
                val = float(val)
            elif config_key == "vault_path":
                val = str(val).strip()
            config[config_key] = val
            updated = True

    if updated:
        config["updated_at"] = _today()

    _save_json(CONFIG_FILE, config)
    print(json.dumps({"status": "ok", "config": config}, ensure_ascii=False))
    return 0


def cmd_writing_add(args):
    """Save an essay with scores."""
    if not CONFIG_FILE.exists():
        print(json.dumps({"status": "error", "message": "Run init first"}))
        return 1

    scores = json.loads(args.scores) if args.scores else {}
    total = args.total if args.total is not None else (sum(scores.values()) / len(scores) if scores else 0)
    total = round(total * 2) / 2

    key_issues = json.loads(args.key_issues) if args.key_issues else []

    slug = _slug(args.topic) if args.topic else "essay"
    filename = f"{_today()}-{slug}.md"
    filepath = WRITING_DIR / filename

    essay_text = args.content or ""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {args.topic or 'Essay'}\n\n")
        f.write(f"**Date:** {_today()}\n")
        f.write(f"**Task Type:** {args.task_type}\n\n")
        f.write(essay_text)

    record = EssayRecord(
        date=_today(),
        task_type=args.task_type or "Task 2",
        topic=args.topic or "",
        word_count=args.word_count or 0,
        scores=scores,
        total=total,
        key_issues=key_issues,
        file=filename,
    )

    index = _load_json(WRITING_DIR / "index.json", [])
    index.append(asdict(record))
    _save_json(WRITING_DIR / "index.json", index)

    # Auto-add key issues to error logbook
    for issue in key_issues:
        _add_error("writing", issue)

    # Record progress
    if total > 0:
        cmd_progress_add_internal("writing", total)

    print(json.dumps({"status": "ok", "record": asdict(record)}, ensure_ascii=False))
    return 0


def cmd_writing_list(args):
    """List essay history."""
    index = _load_json(WRITING_DIR / "index.json", [])
    if args.last and args.last > 0:
        index = index[-args.last:]
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


def cmd_reading_add(args):
    """Save a reading practice record."""
    if not CONFIG_FILE.exists():
        print(json.dumps({"status": "error", "message": "Run init first"}))
        return 1

    question_types = json.loads(args.question_types) if args.question_types else {}
    key_errors = json.loads(args.key_errors) if args.key_errors else []

    total_q = args.total_questions or 0
    correct = args.correct or 0

    record = ReadingRecord(
        date=_today(),
        passage_title=args.passage_title or "",
        total_questions=total_q,
        correct=correct,
        score=args.score or 0.0,
        question_types=question_types,
        synonyms_added=args.synonyms_added or 0,
        key_errors=key_errors,
    )

    index = _load_json(READING_DIR / "index.json", [])
    index.append(asdict(record))
    _save_json(READING_DIR / "index.json", index)

    for err in key_errors:
        _add_error("reading", err)

    if record.score > 0:
        cmd_progress_add_internal("reading", record.score)

    print(json.dumps({"status": "ok", "record": asdict(record)}, ensure_ascii=False))
    return 0


def cmd_listening_add(args):
    """Save a listening practice record."""
    if not CONFIG_FILE.exists():
        print(json.dumps({"status": "error", "message": "Run init first"}))
        return 1

    section_scores = json.loads(args.section_scores) if args.section_scores else {}
    question_type_errors = json.loads(args.question_type_errors) if args.question_type_errors else {}
    key_errors = json.loads(args.key_errors) if args.key_errors else []

    total_q = args.total_questions or 0
    correct = args.correct or 0

    record = ListeningRecord(
        date=_today(),
        test_name=args.test_name or "",
        total_questions=total_q,
        correct=correct,
        score=args.score or 0.0,
        section_scores=section_scores,
        question_type_errors=question_type_errors,
        key_errors=key_errors,
    )

    index = _load_json(LISTENING_DIR / "index.json", [])
    index.append(asdict(record))
    _save_json(LISTENING_DIR / "index.json", index)

    for err in key_errors:
        _add_error("listening", err)

    # Track per-question-type errors
    for qtype, count in question_type_errors.items():
        if count > 0:
            _add_error("listening", f"{qtype}: {count} errors")

    if record.score > 0:
        cmd_progress_add_internal("listening", record.score)

    print(json.dumps({"status": "ok", "record": asdict(record)}, ensure_ascii=False))
    return 0


def cmd_speaking_add(args):
    """Save a speaking practice record."""
    if not CONFIG_FILE.exists():
        print(json.dumps({"status": "error", "message": "Run init first"}))
        return 1

    record = SpeakingRecord(
        date=_today(),
        topic=args.topic or "",
        part=args.part or "Part 2",
        group=args.group or "",
        notes=args.notes or "",
    )

    index = _load_json(SPEAKING_DIR / "index.json", [])
    index.append(asdict(record))
    _save_json(SPEAKING_DIR / "index.json", index)

    print(json.dumps({"status": "ok", "record": asdict(record)}, ensure_ascii=False))
    return 0


def _add_error(category: str, tag: str):
    """Internal: add an error tag to the error logbook."""
    errors = _load_json(ERRORS_FILE, {"writing": [], "reading": [], "listening": [], "speaking": []})
    if category not in errors:
        errors[category] = []

    for item in errors[category]:
        if item["tag"] == tag:
            item["count"] += 1
            item["last_seen"] = _today()
            break
    else:
        errors[category].append({"tag": tag, "count": 1, "first_seen": _today(), "last_seen": _today()})

    _save_json(ERRORS_FILE, errors)


def cmd_error_add(args):
    """Add an error tag to the logbook."""
    _add_error(args.category, args.tag)
    print(json.dumps({"status": "ok", "category": args.category, "tag": args.tag}))
    return 0


def cmd_error_list(args):
    """List errors, optionally filtered by category."""
    errors = _load_json(ERRORS_FILE, {"writing": [], "reading": [], "listening": [], "speaking": []})
    if args.category and args.category in errors:
        result = errors[args.category]
    else:
        result = errors
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_synonym_add(args):
    """Add a synonym pair to the library."""
    synonyms = _load_json(SYNONYMS_FILE, [])

    # Check for duplicates
    for s in synonyms:
        if s["word"].lower() == args.word.lower() and s["synonym"].lower() == args.synonym.lower():
            print(json.dumps({"status": "ok", "message": "Already exists", "entry": s}))
            return 0

    entry = {
        "word": args.word,
        "synonym": args.synonym,
        "source": args.source or "manual",
        "context": args.context or "",
        "date": _today(),
    }
    synonyms.append(entry)
    _save_json(SYNONYMS_FILE, synonyms)
    print(json.dumps({"status": "ok", "entry": entry}))
    return 0


def cmd_synonym_search(args):
    """Search the synonym library."""
    synonyms = _load_json(SYNONYMS_FILE, [])
    query = args.word.lower()
    results = [s for s in synonyms if query in s["word"].lower() or query in s["synonym"].lower()]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_synonym_list(args):
    """List all synonyms."""
    synonyms = _load_json(SYNONYMS_FILE, [])
    print(json.dumps(synonyms, ensure_ascii=False, indent=2))
    return 0


def cmd_progress_add_internal(skill: str, score: float):
    """Internal: record a score without printing."""
    progress = _load_json(PROGRESS_FILE, {
        "writing_scores": [], "reading_scores": [],
        "listening_scores": [], "speaking_scores": [],
    })
    key = f"{skill}_scores"
    if key not in progress:
        progress[key] = []
    progress[key].append({"date": _today(), "score": score})
    _save_json(PROGRESS_FILE, progress)


def cmd_progress_add(args):
    """Record a score for a skill."""
    cmd_progress_add_internal(args.skill, args.score)
    print(json.dumps({"status": "ok", "skill": args.skill, "score": args.score, "date": _today()}))
    return 0


def cmd_progress_show(args):
    """Show progress trends."""
    progress = _load_json(PROGRESS_FILE, {
        "writing_scores": [], "reading_scores": [],
        "listening_scores": [], "speaking_scores": [],
    })
    config = _load_json(CONFIG_FILE, asdict(Config()))

    # Compute trends
    result = {"scores": progress, "target": config.get("target_score", 0), "exam_date": config.get("exam_date", "")}

    for skill in ["writing", "reading", "listening", "speaking"]:
        key = f"{skill}_scores"
        scores = progress.get(key, [])
        if len(scores) >= 2:
            first = scores[0]["score"]
            last = scores[-1]["score"]
            result[f"{skill}_trend"] = round(last - first, 1)
            result[f"{skill}_count"] = len(scores)
            result[f"{skill}_latest"] = last
        elif len(scores) == 1:
            result[f"{skill}_latest"] = scores[0]["score"]
            result[f"{skill}_count"] = 1

    # Error summary
    errors = _load_json(ERRORS_FILE)
    result["error_summary"] = {}
    for cat in ["writing", "reading", "listening", "speaking"]:
        result["error_summary"][cat] = sorted(errors.get(cat, []), key=lambda x: x["count"], reverse=True)[:5]

    # Synonym count
    synonyms = _load_json(SYNONYMS_FILE, [])
    result["synonym_count"] = len(synonyms)

    # Vocab count
    vocab = _load_json(VOCAB_FILE, [])
    result["vocab_count"] = len(vocab)
    result["vocab_due"] = len([v for v in vocab if v.get("next_review", "") <= _today()])

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_vocab_add(args):
    """Add a word to vocabulary."""
    vocab = _load_json(VOCAB_FILE, [])

    # Check duplicate
    for v in vocab:
        if v["word"].lower() == args.word.lower():
            print(json.dumps({"status": "error", "message": f"Word '{args.word}' already exists"}))
            return 1

    synonyms = json.loads(args.synonyms) if args.synonyms else []
    word = VocabWord(
        word=args.word,
        definition=args.definition or "",
        example=args.example or "",
        synonyms=synonyms,
        source=args.source or "",
        date_added=_today(),
        next_review=_today(),  # due immediately
    )
    vocab.append(asdict(word))
    _save_json(VOCAB_FILE, vocab)

    # Also add to synonym library
    for syn in synonyms:
        cmd_synonym_add_internal(args.word, syn, args.source or "vocab")

    print(json.dumps({"status": "ok", "word": asdict(word)}, ensure_ascii=False))
    return 0


def cmd_synonym_add_internal(word: str, synonym: str, source: str = ""):
    """Internal: add synonym without printing, skip duplicates."""
    synonyms = _load_json(SYNONYMS_FILE, [])
    for s in synonyms:
        if s["word"].lower() == word.lower() and s["synonym"].lower() == synonym.lower():
            return
    synonyms.append({"word": word, "synonym": synonym, "source": source, "context": "", "date": _today()})
    _save_json(SYNONYMS_FILE, synonyms)


def cmd_vocab_review(args):
    """Get words due for review (spaced repetition)."""
    vocab = _load_json(VOCAB_FILE, [])
    today = _today()

    due = [v for v in vocab if v.get("next_review", "") <= today]
    due.sort(key=lambda v: v.get("next_review", ""))

    print(json.dumps({"due_count": len(due), "total": len(vocab), "words": due}, ensure_ascii=False, indent=2))
    return 0


def cmd_vocab_update(args):
    """Update a word after review (SM-2 algorithm)."""
    vocab = _load_json(VOCAB_FILE, [])

    for v in vocab:
        if v["word"].lower() == args.word.lower():
            quality = args.quality  # 0-5

            if quality >= 3:
                if v["repetitions"] == 0:
                    v["interval"] = 1
                elif v["repetitions"] == 1:
                    v["interval"] = 6
                else:
                    v["interval"] = round(v["interval"] * v["ease_factor"])

                v["repetitions"] += 1
            else:
                v["interval"] = 1
                v["repetitions"] = 0

            v["ease_factor"] = max(1.3, v["ease_factor"] + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
            v["last_reviewed"] = _today()

            # Calculate next review date
            from datetime import timedelta
            next_date = date.today() + timedelta(days=v["interval"])
            v["next_review"] = next_date.isoformat()

            _save_json(VOCAB_FILE, vocab)
            print(json.dumps({"status": "ok", "word": v}, ensure_ascii=False))
            return 0

    print(json.dumps({"status": "error", "message": f"Word '{args.word}' not found"}))
    return 1


def cmd_vocab_list(args):
    """List all vocabulary words."""
    vocab = _load_json(VOCAB_FILE, [])

    if args.due:
        today = _today()
        vocab = [v for v in vocab if v.get("next_review", "") <= today]

    if args.sort_by == "next_review":
        vocab.sort(key=lambda v: v.get("next_review", ""))
    elif args.sort_by == "date":
        vocab.sort(key=lambda v: v.get("date_added", ""), reverse=True)

    print(json.dumps(vocab, ensure_ascii=False, indent=2))
    return 0


def cmd_dashboard_generate(args):
    """Generate dashboard HTML from template + live data."""
    if not DASHBOARD_TEMPLATE.exists():
        print(json.dumps({"status": "error", "message": f"Template not found: {DASHBOARD_TEMPLATE}"}))
        return 1

    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")

    # Gather all data
    config = _load_json(CONFIG_FILE, asdict(Config()))
    progress = _load_json(PROGRESS_FILE)
    errors = _load_json(ERRORS_FILE)
    synonyms = _load_json(SYNONYMS_FILE, [])
    vocab = _load_json(VOCAB_FILE, [])
    writing_index = _load_json(WRITING_DIR / "index.json", [])
    reading_index = _load_json(READING_DIR / "index.json", [])
    listening_index = _load_json(LISTENING_DIR / "index.json", [])
    speaking_index = _load_json(SPEAKING_DIR / "index.json", [])

    data_json = json.dumps({
        "config": config,
        "progress": progress,
        "errors": errors,
        "synonyms": synonyms,
        "vocab": vocab,
        "writing": writing_index,
        "reading": reading_index,
        "listening": listening_index,
        "speaking": speaking_index,
    }, ensure_ascii=False, default=str)

    # Replace data placeholder in template
    if "___IELTS_DATA___" in template:
        html = template.replace("___IELTS_DATA___", data_json)
    else:
        # Inject data before </body>
        html = template.replace(
            "</body>",
            f'<script id="ielts-data" type="application/json">\n{data_json}\n</script>\n</body>'
        )

    _ensure_dir(IELTS_DIR)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")

    print(json.dumps({"status": "ok", "path": str(DASHBOARD_FILE)}))
    return 0


def cmd_backup(args):
    """Create a zip backup of ~/.ielts/."""
    output = args.output or Path.home() / f"ielts-backup-{_today()}.zip"
    output = Path(output)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(IELTS_DIR):
            for file in files:
                filepath = Path(root) / file
                arcname = filepath.relative_to(IELTS_DIR.parent)
                zf.write(filepath, arcname)

    print(json.dumps({"status": "ok", "backup_path": str(output), "size": output.stat().st_size}))
    return 0


def cmd_restore(args):
    """Restore ~/.ielts/ from a zip backup."""
    backup_path = Path(args.file)
    if not backup_path.exists():
        print(json.dumps({"status": "error", "message": f"Backup file not found: {backup_path}"}))
        return 1

    with zipfile.ZipFile(backup_path, "r") as zf:
        zf.extractall(IELTS_DIR.parent)

    print(json.dumps({"status": "ok", "message": "Restored from " + str(backup_path)}))
    return 0


def cmd_status(args):
    """Output a brief status summary for Claude Code status bar."""
    config = _load_json(CONFIG_FILE, asdict(Config()))
    progress = _load_json(PROGRESS_FILE)

    days = Config(**config).days_until_exam()
    target = config.get("target_score", 0)

    latest = {}
    for skill in ["writing", "reading", "listening", "speaking"]:
        scores = progress.get(f"{skill}_scores", [])
        if scores:
            latest[skill] = scores[-1]["score"]

    vocab = _load_json(VOCAB_FILE, [])
    vocab_due = len([v for v in vocab if v.get("next_review", "") <= _today()])

    parts = []
    if days > 0:
        parts.append(f"📅 {days}d")
    if target > 0:
        parts.append(f"🎯 {target}")
    if latest:
        score_str = " | ".join(f"{k[0].upper()}:{v}" for k, v in latest.items())
        parts.append(score_str)
    if vocab_due > 0:
        parts.append(f"📝 {vocab_due} words")

    print("IELTS " + " · ".join(parts) if parts else "IELTS: run /ielts to set up")
    return 0


# ── Memory Commands ────────────────────────────────────────────────

def cmd_memory_add(args):
    """Save a coaching observation to persistent memory."""
    memories = _load_json(MEMORY_FILE, [])
    import hashlib
    memory_id = hashlib.md5(f"{datetime.now().isoformat()}{args.content}".encode()).hexdigest()[:8]

    entry = CoachMemory(
        id=memory_id,
        date=_today(),
        category=args.category or "note",
        skill=args.skill or "general",
        content=args.content,
        source=args.source or "manual",
        priority=args.priority or "medium",
    )
    memories.append(asdict(entry))
    _save_json(MEMORY_FILE, memories)
    print(json.dumps({"status": "ok", "memory": asdict(entry)}, ensure_ascii=False))
    return 0


def cmd_memory_list(args):
    """List coaching memories with optional filters."""
    memories = _load_json(MEMORY_FILE, [])
    if args.category:
        memories = [m for m in memories if m["category"] == args.category]
    if args.skill:
        memories = [m for m in memories if m["skill"] == args.skill]
    memories.sort(key=lambda m: m["date"], reverse=True)
    if args.last and args.last > 0:
        memories = memories[:args.last]
    print(json.dumps(memories, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_search(args):
    """Search coaching memories by keyword."""
    memories = _load_json(MEMORY_FILE, [])
    query = args.query.lower()
    results = [m for m in memories if query in m["content"].lower()]
    results.sort(key=lambda m: m["date"], reverse=True)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_delete(args):
    """Delete a coaching memory by ID."""
    memories = _load_json(MEMORY_FILE, [])
    before = len(memories)
    memories = [m for m in memories if m["id"] != args.id]
    _save_json(MEMORY_FILE, memories)
    print(json.dumps({"status": "ok", "message": f"Deleted {before - len(memories)} memory"}))
    return 0


# ── Obsidian Vault Sync ────────────────────────────────────────────
#
# JSON under ~/.ielts stays the single source of truth. The vault holds a
# Markdown *projection* of it. SM-2 state and error counts are only ever
# written by this CLI, never read back from the vault.
#
# Everything here is fail-soft: if vault_path is unset / missing / unwritable
# (e.g. the NAS is not mounted), the commands print {"status":"skipped",...}
# and exit 0 so the calling skill never breaks.

VAULT_ROOT_NAME = "IELTS"
GEN_START = "<!-- ielts:generated:start -->"
GEN_END = "<!-- ielts:generated:end -->"

EXPORT_TYPES = ["memory", "writing", "vocab", "synonym", "speaking", "progress", "errors"]
IMPORT_TYPES = ["vocab", "memory"]

MEMORY_CATEGORIES = ["observation", "preference", "weakness", "strength", "strategy", "note"]
MEMORY_SKILLS = ["general", "writing", "reading", "listening", "speaking", "vocab"]
MEMORY_PRIORITIES = ["high", "medium", "low"]

_FM_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_\-. ]*):(?:\s|$)")


# ── Frontmatter / note primitives ──────────────────────────────────

def _safe_filename(text: str, max_len: int = 80) -> str:
    """Filesystem-safe note filename. Keeps unicode, drops path-hostile chars."""
    bad = set('/\\:*?"<>|')
    out = "".join(" " if (c in bad or ord(c) < 32) else c for c in str(text))
    out = " ".join(out.split()).strip(" .")
    return out[:max_len] or "untitled"


def _yaml_scalar(value) -> str:
    """Serialize a scalar for YAML frontmatter (stdlib-only, no pyyaml)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    needs_quote = (
        s == ""
        or s != s.strip()
        or s.lower() in ("true", "false", "null", "yes", "no", "on", "off")
        or s[0] in "-?:,[]{}#&*!|>'\"%@`"
        or ": " in s
        or s.endswith(":")
        or "#" in s
        or "\n" in s
    )
    if needs_quote:
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", " ").replace("\r", " ")
        return '"' + s + '"'
    return s


def _parse_scalar(raw: str):
    """Very small YAML scalar reader — enough for the keys we own."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s[0] == '"' and s.endswith('"') and len(s) >= 2:
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return s
    if s[0] == "'" and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    return s


def _fm_lines(key: str, value) -> list:
    """Render one frontmatter entry as a list of raw lines."""
    if isinstance(value, dict):
        if not value:
            return [f"{key}: {{}}"]
        lines = [f"{key}:"]
        for k, v in value.items():
            lines.append(f"  {k}: {_yaml_scalar(v)}")
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{key}: []"]
        return [f"{key}:"] + [f"  - {_yaml_scalar(v)}" for v in value]
    return [f"{key}: {_yaml_scalar(value)}"]


def _split_note(text: str):
    """Split a note into (frontmatter_entries, body).

    frontmatter_entries is an ordered list of (key, raw_lines) so that keys the
    CLI does not own survive a round-trip byte-for-byte.
    """
    if not text.startswith("---\n") and text != "---":
        return [], text

    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return [], text

    entries = []
    for line in lines[1:end]:
        m = _FM_KEY_RE.match(line)
        if m and not line.startswith((" ", "\t", "-")):
            entries.append((m.group(1).strip(), [line]))
        elif entries:
            entries[-1][1].append(line)
        # a stray continuation with no key is dropped (malformed frontmatter)

    body = "\n".join(lines[end + 1:])
    return entries, body.lstrip("\n")


def _fm_get(entries, key, default=None):
    """Read a single-line frontmatter value from parsed entries."""
    for k, raw_lines in entries:
        if k == key:
            _, _, raw = raw_lines[0].partition(":")
            if len(raw_lines) > 1:
                items = [ln.strip()[2:].strip() for ln in raw_lines[1:] if ln.strip().startswith("- ")]
                if items:
                    return [_parse_scalar(i) for i in items]
            return _parse_scalar(raw)
    return default


def _merge_frontmatter(existing_entries, owned_pairs):
    """CLI-owned keys overwrite; any other key the user added is preserved."""
    owned_map = {}
    for k, v in owned_pairs:
        owned_map[k] = _fm_lines(k, v)

    result, seen = [], set()
    for k, raw_lines in existing_entries:
        if k in owned_map and k not in seen:
            result.append((k, owned_map[k]))
            seen.add(k)
        elif k in seen:
            continue  # drop duplicate of an owned key
        else:
            result.append((k, raw_lines))
    for k, _v in owned_pairs:
        if k not in seen:
            result.append((k, owned_map[k]))
            seen.add(k)
    return result


def _merge_body(existing_body: str, generated: str) -> str:
    """Replace only what is between the generated markers; keep the rest verbatim."""
    block = GEN_START + "\n" + generated.strip("\n") + "\n" + GEN_END
    start = existing_body.find(GEN_START)
    if start != -1:
        end = existing_body.find(GEN_END, start)
        if end != -1:
            return existing_body[:start] + block + existing_body[end + len(GEN_END):]
    if existing_body.strip():
        return existing_body.rstrip("\n") + "\n\n" + block + "\n"
    return block + "\n"


def _render_note(entries, body: str) -> str:
    out = ["---"]
    for _k, raw_lines in entries:
        out.extend(raw_lines)
    out.append("---")
    out.append("")
    text = "\n".join(out) + body.lstrip("\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _write_note(path: Path, owned_pairs, generated_body: str, stats: dict, dry_run: bool):
    """Create or update one note. Returns 'created' | 'updated' | 'unchanged'."""
    old_text = None
    if path.exists():
        try:
            old_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old_text = None

    entries, body = _split_note(old_text) if old_text is not None else ([], "")
    new_text = _render_note(_merge_frontmatter(entries, owned_pairs), _merge_body(body, generated_body))

    if old_text is not None and old_text == new_text:
        stats["unchanged"] += 1
        return "unchanged"

    action = "updated" if old_text is not None else "created"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    stats[action] += 1
    return action


def _md_escape(text) -> str:
    """Make a value safe inside a markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


# ── Vault availability ─────────────────────────────────────────────

def _vault_root():
    """Return (root_path, reason). root_path is None when the vault is unusable."""
    config = _load_json(CONFIG_FILE, {}) or {}
    raw = str(config.get("vault_path", "") or "").strip()
    if not raw:
        return None, "vault_path is not configured (run: config set --vault-path <path>)"
    try:
        base = Path(raw).expanduser()
        if not base.exists():
            return None, f"vault path does not exist (NAS not mounted?): {base}"
        if not base.is_dir():
            return None, f"vault path is not a directory: {base}"
        if not os.access(base, os.W_OK):
            return None, f"vault path is not writable: {base}"
    except OSError as e:
        return None, f"vault path is not accessible: {e}"
    return base / VAULT_ROOT_NAME, ""


def _parse_only(value, allowed):
    if not value:
        return list(allowed)
    picked = [t.strip() for t in str(value).split(",") if t.strip()]
    return [t for t in picked if t in allowed]


# ── Source data → note payloads ────────────────────────────────────

def _essay_body(record: dict) -> str:
    """Read the archived essay text, stripping the header writing add wrote."""
    filename = record.get("file", "")
    if not filename:
        return ""
    path = WRITING_DIR / filename
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.split("\n")
    i = 0
    if i < len(lines) and lines[i].startswith("# "):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    saw_header = False
    if i < len(lines) and lines[i].startswith("**Date:**"):
        i += 1
        saw_header = True
    if i < len(lines) and lines[i].startswith("**Task Type:**"):
        i += 1
        saw_header = True
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:]).strip() if saw_header else text.strip()


def _writing_items():
    """[(note_id, record)] with stable, unique ids."""
    index = _load_json(WRITING_DIR / "index.json", []) or []
    items, used = [], set()
    for rec in index:
        filename = rec.get("file", "")
        base = filename[:-3] if filename.endswith(".md") else ""
        if not base:
            base = f"{rec.get('date', _today())}-{_slug(rec.get('topic', '') or 'essay')}"
        nid, n = base, 2
        while nid in used:
            nid = f"{base}-{n}"
            n += 1
        used.add(nid)
        items.append((nid, rec))
    return items


def _speaking_items():
    index = _load_json(SPEAKING_DIR / "index.json", []) or []
    items, used = [], set()
    for rec in index:
        base = f"{rec.get('date', _today())}-{_slug(rec.get('topic', '') or 'speaking')}"
        nid, n = base, 2
        while nid in used:
            nid = f"{base}-{n}"
            n += 1
        used.add(nid)
        items.append((nid, rec))
    return items


def _synonym_groups():
    """{word: [ {synonym, source, context, date}, ... ]} from synonyms.json."""
    groups = {}
    for entry in _load_json(SYNONYMS_FILE, []) or []:
        word = str(entry.get("word", "")).strip()
        if not word:
            continue
        groups.setdefault(word, []).append(entry)
    return groups


def _vocab_synonyms(word: str, vocab_entry: dict, groups: dict) -> list:
    """Union of the word's own synonyms list and the synonym library."""
    out, seen = [], set()
    for s in (vocab_entry.get("synonyms") or []):
        s = str(s).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    for key, entries in groups.items():
        if key.lower() != word.lower():
            continue
        for e in entries:
            s = str(e.get("synonym", "")).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


# ── Note body generators ───────────────────────────────────────────

def _body_memory(mem: dict) -> str:
    lines = [str(mem.get("content", "")).strip(), ""]
    tags = f"#ielts/memory/{mem.get('category', 'note')} #ielts/skill/{mem.get('skill', 'general')}"
    lines.append(tags)
    return "\n".join(lines)


def _body_writing(rec: dict) -> str:
    scores = rec.get("scores", {}) or {}
    lines = [f"# {rec.get('topic') or 'Essay'}", ""]
    lines.append(f"- **Task:** {rec.get('task_type', '')}　**Words:** {rec.get('word_count', 0)}　**Total:** {rec.get('total', 0)}")
    if scores:
        lines.append("- **Scores:** " + "　".join(f"{k} {v}" for k, v in scores.items()))
    issues = rec.get("key_issues") or []
    if issues:
        lines.append("")
        lines.append("## Key issues")
        lines.extend(f"- {i}" for i in issues)
    body = _essay_body(rec)
    lines.append("")
    lines.append("## Essay")
    lines.append("")
    lines.append(body if body else "_(essay file not found in ~/.ielts/writing/)_")
    return "\n".join(lines)


def _body_vocab(v: dict, synonyms: list) -> str:
    lines = [f"# {v.get('word', '')}", ""]
    if v.get("definition"):
        lines.append(str(v["definition"]))
        lines.append("")
    if v.get("example"):
        lines.append(f"> {v['example']}")
        lines.append("")
    lines.append("## Synonyms")
    if synonyms:
        lines.extend(f"- [[{s}]]" for s in synonyms)
    else:
        lines.append("_(none yet)_")
    lines.append("")
    lines.append("## Review")
    lines.append(
        f"- next: {v.get('next_review', '') or '-'}　interval: {v.get('interval', 0)}d"
        f"　reps: {v.get('repetitions', 0)}　EF: {v.get('ease_factor', 2.5)}"
    )
    return "\n".join(lines)


def _body_synonym(word: str, entries: list) -> str:
    lines = [f"# {word}", "", "## Synonyms", ""]
    seen = set()
    for e in entries:
        s = str(e.get("synonym", "")).strip()
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        extra = []
        if e.get("source"):
            extra.append(str(e["source"]))
        if e.get("context"):
            extra.append(str(e["context"]))
        suffix = f" — {' · '.join(extra)}" if extra else ""
        lines.append(f"- [[{s}]]{suffix}")
    if not seen:
        lines.append("_(none)_")
    lines.append("")
    lines.append(f"Back to [[{word}]] in vocab.")
    return "\n".join(lines)


def _body_speaking(rec: dict) -> str:
    lines = [f"# {rec.get('topic') or 'Speaking'}", ""]
    lines.append(f"- **Part:** {rec.get('part', '')}　**Group:** {rec.get('group', '') or '-'}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(str(rec.get("notes", "")).strip() or "_(no notes)_")
    return "\n".join(lines)


def _body_progress() -> str:
    config = _load_json(CONFIG_FILE, asdict(Config())) or {}
    progress = _load_json(PROGRESS_FILE, {}) or {}
    cfg = Config(**{k: v for k, v in config.items() if k in Config.__dataclass_fields__})

    lines = ["# 進度", "", "## 目標", "", "| 項目 | 值 |", "| --- | --- |"]
    lines.append(f"| 目標分 | {config.get('target_score', 0)} |")
    lines.append(f"| 考試日期 | {config.get('exam_date', '') or '-'} |")
    lines.append(f"| 剩餘天數 | {cfg.days_until_exam()} |")
    lines.append(f"| 自評總分 | {cfg.overall_band()} |")
    for skill in ["listening", "reading", "writing", "speaking"]:
        lines.append(f"| 自評 {skill} | {config.get(skill, 0)} |")

    lines += ["", "## 分數趨勢", "", "| 科目 | 次數 | 最早 | 最新 | 變化 |", "| --- | --- | --- | --- | --- |"]
    for skill in ["writing", "reading", "listening", "speaking"]:
        scores = progress.get(f"{skill}_scores", []) or []
        if not scores:
            lines.append(f"| {skill} | 0 | - | - | - |")
            continue
        first, last = scores[0]["score"], scores[-1]["score"]
        delta = round(last - first, 1)
        lines.append(f"| {skill} | {len(scores)} | {first} | {last} | {delta:+} |")

    for skill in ["writing", "reading", "listening", "speaking"]:
        scores = progress.get(f"{skill}_scores", []) or []
        if not scores:
            continue
        lines += ["", f"### {skill} 逐次記錄", "", "| 日期 | 分數 |", "| --- | --- |"]
        lines += [f"| {s.get('date', '')} | {s.get('score', '')} |" for s in scores]

    reading = _load_json(READING_DIR / "index.json", []) or []
    lines += ["", "## 閱讀記錄", ""]
    if reading:
        lines += ["| 日期 | 文章 | 對/總 | Band | 同義替換 |", "| --- | --- | --- | --- | --- |"]
        for r in reading:
            lines.append(
                f"| {r.get('date', '')} | {_md_escape(r.get('passage_title', ''))} | "
                f"{r.get('correct', 0)}/{r.get('total_questions', 0)} | {r.get('score', 0)} | "
                f"{r.get('synonyms_added', 0)} |"
            )
    else:
        lines.append("_(尚無記錄)_")

    listening = _load_json(LISTENING_DIR / "index.json", []) or []
    lines += ["", "## 聽力記錄", ""]
    if listening:
        lines += ["| 日期 | 測驗 | 對/總 | Band |", "| --- | --- | --- | --- |"]
        for r in listening:
            lines.append(
                f"| {r.get('date', '')} | {_md_escape(r.get('test_name', ''))} | "
                f"{r.get('correct', 0)}/{r.get('total_questions', 0)} | {r.get('score', 0)} |"
            )
    else:
        lines.append("_(尚無記錄)_")

    return "\n".join(lines)


def _body_errors() -> str:
    errors = _load_json(ERRORS_FILE, {}) or {}
    lines = ["# 錯題本", ""]
    total = 0
    for cat in ["writing", "reading", "listening", "speaking"]:
        items = sorted(errors.get(cat, []) or [], key=lambda x: x.get("count", 0), reverse=True)
        total += len(items)
        lines += ["", f"## {cat}", ""]
        if not items:
            lines.append("_(尚無記錄)_")
            continue
        lines += ["| 次數 | 標籤 | 首次 | 最近 |", "| --- | --- | --- | --- |"]
        for it in items:
            lines.append(
                f"| {it.get('count', 0)} | {_md_escape(it.get('tag', ''))} | "
                f"{it.get('first_seen', '')} | {it.get('last_seen', '')} |"
            )
    if total == 0:
        lines.insert(1, "_(尚無錯題記錄)_")
    return "\n".join(lines)


def _body_moc(counts: dict) -> str:
    memories = _load_json(MEMORY_FILE, []) or []
    vocab = _load_json(VOCAB_FILE, []) or []
    groups = _synonym_groups()
    due = len([v for v in vocab if str(v.get("next_review", "")) <= _today()])

    lines = ["# IELTS", "", "## 摘要", "", "| 項目 | 數量 |", "| --- | --- |"]
    lines.append(f"| 作文 | {counts.get('writing', 0)} |")
    lines.append(f"| 口說 | {counts.get('speaking', 0)} |")
    lines.append(f"| 單字 | {len(vocab)} |")
    lines.append(f"| 今日待複習 | {due} |")
    lines.append(f"| 同義替換詞頭 | {len(groups)} |")
    lines.append(f"| 教練記憶 | {len(memories)} |")

    lines += ["", "## 總覽", "", f"- [[{VAULT_ROOT_NAME}/進度|進度]]", f"- [[{VAULT_ROOT_NAME}/錯題本|錯題本]]"]

    def section(title, links):
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        if links:
            lines.extend(links)
        else:
            lines.append("_(尚無)_")

    section("作文", [
        f"- [[{VAULT_ROOT_NAME}/writing/{nid}|{rec.get('date', '')} {rec.get('topic') or 'Essay'}]] — {rec.get('total', 0)}"
        for nid, rec in _writing_items()
    ])
    section("口說", [
        f"- [[{VAULT_ROOT_NAME}/speaking/{nid}|{rec.get('date', '')} {rec.get('topic') or 'Speaking'}]]"
        for nid, rec in _speaking_items()
    ])
    section("教練記憶", [
        f"- [[{VAULT_ROOT_NAME}/memory/{m.get('date', '')}-{m.get('id', '')}|{m.get('date', '')} {m.get('category', '')}]]"
        f" — {_md_escape(str(m.get('content', ''))[:60])}"
        for m in sorted(memories, key=lambda x: str(x.get("date", "")), reverse=True)
    ])
    section("單字", ["- " + "、".join(f"[[{v.get('word', '')}]]" for v in vocab)] if vocab else [])
    section("同義替換", ["- " + "、".join(f"[[{VAULT_ROOT_NAME}/synonym/{w}|{w}]]" for w in sorted(groups))] if groups else [])

    return "\n".join(lines)


# ── vault export ───────────────────────────────────────────────────

def _export(root: Path, types, dry_run: bool) -> dict:
    stats = {"created": 0, "updated": 0, "unchanged": 0}
    per_type = {}

    def track(name, fn):
        before = dict(stats)
        fn()
        per_type[name] = {k: stats[k] - before[k] for k in stats}

    if "memory" in types:
        def do_memory():
            for m in _load_json(MEMORY_FILE, []) or []:
                mid = m.get("id", "")
                mdate = m.get("date", "") or _today()
                path = root / "memory" / f"{_safe_filename(f'{mdate}-{mid}')}.md"
                owned = [
                    ("date", mdate),
                    ("category", m.get("category", "note")),
                    ("skill", m.get("skill", "general")),
                    ("priority", m.get("priority", "medium")),
                    ("source", m.get("source", "")),
                    ("tags", [f"ielts/memory/{m.get('category', 'note')}", f"ielts/skill/{m.get('skill', 'general')}"]),
                    ("ielts_type", "memory"),
                    ("ielts_id", mid),
                    ("ielts_managed", True),
                ]
                _write_note(path, owned, _body_memory(m), stats, dry_run)
        track("memory", do_memory)

    if "writing" in types:
        def do_writing():
            for nid, rec in _writing_items():
                path = root / "writing" / f"{_safe_filename(nid)}.md"
                owned = [
                    ("date", rec.get("date", "")),
                    ("task_type", rec.get("task_type", "")),
                    ("topic", rec.get("topic", "")),
                    ("word_count", rec.get("word_count", 0)),
                    ("scores", rec.get("scores", {}) or {}),
                    ("total", rec.get("total", 0)),
                    ("key_issues", rec.get("key_issues", []) or []),
                    ("tags", ["ielts/writing"]),
                    ("ielts_type", "writing"),
                    ("ielts_id", nid),
                    ("ielts_managed", True),
                ]
                _write_note(path, owned, _body_writing(rec), stats, dry_run)
        track("writing", do_writing)

    groups = _synonym_groups()

    if "vocab" in types:
        def do_vocab():
            for v in _load_json(VOCAB_FILE, []) or []:
                word = str(v.get("word", "")).strip()
                if not word:
                    continue
                syns = _vocab_synonyms(word, v, groups)
                path = root / "vocab" / f"{_safe_filename(word)}.md"
                owned = [
                    ("word", word),
                    ("definition", v.get("definition", "")),
                    ("example", v.get("example", "")),
                    ("date_added", v.get("date_added", "")),
                    ("source", v.get("source", "")),
                    ("ease_factor", v.get("ease_factor", 2.5)),
                    ("interval", v.get("interval", 0)),
                    ("repetitions", v.get("repetitions", 0)),
                    ("next_review", v.get("next_review", "")),
                    ("last_reviewed", v.get("last_reviewed", "")),
                    ("tags", ["ielts/vocab"]),
                    ("ielts_type", "vocab"),
                    ("ielts_id", word.lower()),
                    ("ielts_managed", True),
                ]
                _write_note(path, owned, _body_vocab(v, syns), stats, dry_run)
        track("vocab", do_vocab)

    if "synonym" in types:
        def do_synonym():
            for word in sorted(groups):
                entries = groups[word]
                path = root / "synonym" / f"{_safe_filename(word)}.md"
                owned = [
                    ("word", word),
                    ("synonym_count", len({str(e.get("synonym", "")).lower() for e in entries})),
                    ("tags", ["ielts/synonym"]),
                    ("ielts_type", "synonym"),
                    ("ielts_id", word.lower()),
                    ("ielts_managed", True),
                ]
                _write_note(path, owned, _body_synonym(word, entries), stats, dry_run)
        track("synonym", do_synonym)

    if "speaking" in types:
        def do_speaking():
            for nid, rec in _speaking_items():
                path = root / "speaking" / f"{_safe_filename(nid)}.md"
                owned = [
                    ("date", rec.get("date", "")),
                    ("topic", rec.get("topic", "")),
                    ("part", rec.get("part", "")),
                    ("group", rec.get("group", "")),
                    ("tags", ["ielts/speaking"]),
                    ("ielts_type", "speaking"),
                    ("ielts_id", nid),
                    ("ielts_managed", True),
                ]
                _write_note(path, owned, _body_speaking(rec), stats, dry_run)
        track("speaking", do_speaking)

    if "progress" in types:
        def do_progress():
            owned = [
                ("ielts_type", "progress"),
                ("ielts_id", "progress"),
                ("ielts_managed", True),
                ("tags", ["ielts/progress"]),
            ]
            _write_note(root / "進度.md", owned, _body_progress(), stats, dry_run)
        track("progress", do_progress)

    if "errors" in types:
        def do_errors():
            owned = [
                ("ielts_type", "errors"),
                ("ielts_id", "errors"),
                ("ielts_managed", True),
                ("tags", ["ielts/errors"]),
            ]
            _write_note(root / "錯題本.md", owned, _body_errors(), stats, dry_run)
        track("errors", do_errors)

    # The MOC always regenerates so its links/counts stay in sync.
    def do_moc():
        counts = {
            "writing": len(_writing_items()),
            "speaking": len(_speaking_items()),
        }
        owned = [
            ("ielts_type", "moc"),
            ("ielts_id", "moc"),
            ("ielts_managed", True),
            ("tags", ["ielts/moc"]),
        ]
        _write_note(root / f"{VAULT_ROOT_NAME}.md", owned, _body_moc(counts), stats, dry_run)
    track("moc", do_moc)

    return {"totals": stats, "by_type": per_type}


def cmd_vault_export(args):
    """Project ~/.ielts JSON into the Obsidian vault as Markdown notes."""
    try:
        root, reason = _vault_root()
        if root is None:
            print(json.dumps({"status": "skipped", "reason": reason}, ensure_ascii=False))
            return 0

        types = _parse_only(getattr(args, "only", ""), EXPORT_TYPES)
        if not types:
            print(json.dumps({"status": "skipped", "reason": "no valid --only types given"}, ensure_ascii=False))
            return 0

        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)

        result = _export(root, types, dry_run)
        print(json.dumps({
            "status": "ok",
            "dry_run": dry_run,
            "root": str(root),
            "types": types,
            **result,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:  # fail-soft: never break the calling skill
        print(json.dumps({"status": "skipped", "reason": f"export failed: {e}"}, ensure_ascii=False))
        return 0


# ── vault import ───────────────────────────────────────────────────

def _body_text_only(body: str) -> str:
    """Body with any generated block removed."""
    start = body.find(GEN_START)
    if start != -1:
        end = body.find(GEN_END, start)
        if end != -1:
            body = body[:start] + body[end + len(GEN_END):]
    return body.strip()


def _is_managed(entries) -> bool:
    return bool(_fm_get(entries, "ielts_id")) or _fm_get(entries, "ielts_managed") is True


def _scan_unmanaged(folder: Path):
    """Yield (path, entries, body) for notes the CLI does not already own."""
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entries, body = _split_note(text)
        if _is_managed(entries):
            continue
        yield path, entries, body


def cmd_vault_import(args):
    """Pull manually-created vault notes back into the JSON store."""
    try:
        root, reason = _vault_root()
        if root is None:
            print(json.dumps({"status": "skipped", "reason": reason}, ensure_ascii=False))
            return 0

        types = _parse_only(getattr(args, "only", ""), IMPORT_TYPES)
        dry_run = bool(getattr(args, "dry_run", False))
        result = {"vocab": {"imported": [], "skipped": []}, "memory": {"imported": [], "skipped": []}}
        renames = []

        if "vocab" in types:
            vocab = _load_json(VOCAB_FILE, []) or []
            known = {str(v.get("word", "")).lower() for v in vocab}
            added = False
            for path, entries, body in _scan_unmanaged(root / "vocab"):
                word = str(_fm_get(entries, "word", "") or "").strip() or path.stem
                if word.lower() in known:
                    result["vocab"]["skipped"].append({"file": path.name, "reason": "word already in vocab.json"})
                    continue
                text = _body_text_only(body)
                definition = str(_fm_get(entries, "definition", "") or "").strip()
                if not definition:
                    for line in text.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            definition = line
                            break
                raw_syn = _fm_get(entries, "synonyms", []) or []
                syns = [str(s).strip() for s in raw_syn if str(s).strip()] if isinstance(raw_syn, list) else []
                entry = VocabWord(
                    word=word,
                    definition=definition,
                    example=str(_fm_get(entries, "example", "") or ""),
                    synonyms=syns,
                    source=str(_fm_get(entries, "source", "") or "obsidian"),
                    date_added=str(_fm_get(entries, "date_added", "") or _today()),
                    next_review=_today(),
                )
                known.add(word.lower())
                result["vocab"]["imported"].append({"file": path.name, "word": word})
                if not dry_run:
                    vocab.append(asdict(entry))
                    added = True
                    for s in syns:
                        cmd_synonym_add_internal(word, s, "obsidian")
                canonical = root / "vocab" / f"{_safe_filename(word)}.md"
                if canonical != path and not canonical.exists():
                    renames.append((path, canonical))
            if added and not dry_run:
                _save_json(VOCAB_FILE, vocab)

        if "memory" in types:
            memories = _load_json(MEMORY_FILE, []) or []
            known_content = {str(m.get("content", "")).strip() for m in memories}
            added = False
            for path, entries, body in _scan_unmanaged(root / "memory"):
                content = str(_fm_get(entries, "content", "") or "").strip()
                if not content:
                    text = _body_text_only(body)
                    lines = [ln for ln in text.split("\n") if not ln.strip().startswith("#ielts/")]
                    if lines and lines[0].startswith("# "):
                        lines = lines[1:]
                    content = "\n".join(lines).strip()
                if not content:
                    result["memory"]["skipped"].append({"file": path.name, "reason": "empty content"})
                    continue
                if content in known_content:
                    result["memory"]["skipped"].append({"file": path.name, "reason": "identical memory already exists"})
                    continue

                def pick(key, allowed, fallback):
                    val = str(_fm_get(entries, key, "") or "").strip().lower()
                    return val if val in allowed else fallback

                mdate = str(_fm_get(entries, "date", "") or "").strip() or _today()
                mid = hashlib.md5(f"{path.name}{content}".encode("utf-8")).hexdigest()[:8]
                mem = CoachMemory(
                    id=mid,
                    date=mdate,
                    category=pick("category", MEMORY_CATEGORIES, "note"),
                    skill=pick("skill", MEMORY_SKILLS, "general"),
                    content=content,
                    source=str(_fm_get(entries, "source", "") or "obsidian"),
                    priority=pick("priority", MEMORY_PRIORITIES, "medium"),
                )
                known_content.add(content)
                result["memory"]["imported"].append({"file": path.name, "id": mid})
                if not dry_run:
                    memories.append(asdict(mem))
                    added = True
                canonical = root / "memory" / f"{_safe_filename(f'{mdate}-{mid}')}.md"
                if canonical != path and not canonical.exists():
                    renames.append((path, canonical))
            if added and not dry_run:
                _save_json(MEMORY_FILE, memories)

        renamed = []
        if not dry_run:
            for src, dst in renames:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(src), str(dst))
                    renamed.append({"from": src.name, "to": dst.name})
                except OSError as e:
                    result.setdefault("errors", []).append(f"rename {src.name}: {e}")

        # Re-export so the freshly imported notes get their ids and managed flag.
        export_result = None
        imported_any = bool(result["vocab"]["imported"] or result["memory"]["imported"])
        if imported_any and not dry_run:
            export_types = [t for t in types if t in EXPORT_TYPES]
            if "vocab" in export_types:
                export_types.append("synonym")
            export_result = _export(root, export_types, False)

        print(json.dumps({
            "status": "ok",
            "dry_run": dry_run,
            "root": str(root),
            "types": types,
            "imported": {
                "vocab": len(result["vocab"]["imported"]),
                "memory": len(result["memory"]["imported"]),
            },
            "skipped": {
                "vocab": len(result["vocab"]["skipped"]),
                "memory": len(result["memory"]["skipped"]),
            },
            "detail": result,
            "renamed": renamed,
            "re_export": export_result,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:  # fail-soft
        print(json.dumps({"status": "skipped", "reason": f"import failed: {e}"}, ensure_ascii=False))
        return 0


def cmd_vault_status(args):
    """Report vault reachability and per-type counts."""
    try:
        config = _load_json(CONFIG_FILE, {}) or {}
        vault_path = str(config.get("vault_path", "") or "")
        root, reason = _vault_root()
        if root is None:
            print(json.dumps({
                "status": "skipped",
                "available": False,
                "vault_path": vault_path,
                "reason": reason,
            }, ensure_ascii=False, indent=2))
            return 0

        def note_count(sub):
            d = root / sub
            return len(list(d.glob("*.md"))) if d.is_dir() else 0

        vocab = _load_json(VOCAB_FILE, []) or []
        groups = _synonym_groups()
        types = {
            "memory": {"source": len(_load_json(MEMORY_FILE, []) or []), "notes": note_count("memory")},
            "writing": {"source": len(_writing_items()), "notes": note_count("writing")},
            "vocab": {"source": len(vocab), "notes": note_count("vocab")},
            "synonym": {"source": len(groups), "notes": note_count("synonym")},
            "speaking": {"source": len(_speaking_items()), "notes": note_count("speaking")},
            "progress": {"source": 1, "notes": 1 if (root / "進度.md").exists() else 0},
            "errors": {"source": 1, "notes": 1 if (root / "錯題本.md").exists() else 0},
        }

        unmanaged = {
            "vocab": len(list(_scan_unmanaged(root / "vocab"))),
            "memory": len(list(_scan_unmanaged(root / "memory"))),
        }

        print(json.dumps({
            "status": "ok",
            "available": True,
            "vault_path": vault_path,
            "root": str(root),
            "root_exists": root.exists(),
            "types": types,
            "unmanaged_notes": unmanaged,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:  # fail-soft
        print(json.dumps({"status": "skipped", "reason": f"status failed: {e}"}, ensure_ascii=False))
        return 0


# ── CLI Main ───────────────────────────────────────────────────────

def main():
    _resolve_paths()  # honour IELTS_HOME at every invocation
    parser = argparse.ArgumentParser(description="IELTS Claude Skills Data Layer")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub.add_parser("init", help="Initialize ~/.ielts/ directory")

    # config
    p_config = sub.add_parser("config", help="Config management")
    p_config_sub = p_config.add_subparsers(dest="config_action")
    p_config_sub.add_parser("get", help="Read config")
    p_set = p_config_sub.add_parser("set", help="Update config")
    p_set.add_argument("--target-score", type=float)
    p_set.add_argument("--exam-date")
    p_set.add_argument("--listening", type=float)
    p_set.add_argument("--reading", type=float)
    p_set.add_argument("--writing", type=float)
    p_set.add_argument("--speaking", type=float)
    p_set.add_argument("--name")
    p_set.add_argument("--vault-path", help="Obsidian vault root (empty string disables vault sync)")

    # writing
    p_writing = sub.add_parser("writing", help="Writing data management")
    p_writing_sub = p_writing.add_subparsers(dest="writing_action")
    p_wa = p_writing_sub.add_parser("add", help="Save an essay")
    p_wa.add_argument("--task-type", default="Task 2")
    p_wa.add_argument("--topic", default="")
    p_wa.add_argument("--word-count", type=int, default=0)
    p_wa.add_argument("--scores", default="{}")
    p_wa.add_argument("--total", type=float)
    p_wa.add_argument("--key-issues", default="[]")
    p_wa.add_argument("--content", default="")
    p_wl = p_writing_sub.add_parser("list", help="List essays")
    p_wl.add_argument("--last", type=int, default=0)

    # reading
    p_reading = sub.add_parser("reading", help="Reading data management")
    p_reading_sub = p_reading.add_subparsers(dest="reading_action")
    p_ra = p_reading_sub.add_parser("add", help="Save a reading record")
    p_ra.add_argument("--passage-title", default="")
    p_ra.add_argument("--total-questions", type=int, default=0)
    p_ra.add_argument("--correct", type=int, default=0)
    p_ra.add_argument("--score", type=float, default=0.0)
    p_ra.add_argument("--question-types", default="{}")
    p_ra.add_argument("--synonyms-added", type=int, default=0)
    p_ra.add_argument("--key-errors", default="[]")

    # listening
    p_listening = sub.add_parser("listening", help="Listening data management")
    p_listening_sub = p_listening.add_subparsers(dest="listening_action")
    p_la = p_listening_sub.add_parser("add", help="Save a listening record")
    p_la.add_argument("--test-name", default="")
    p_la.add_argument("--total-questions", type=int, default=0)
    p_la.add_argument("--correct", type=int, default=0)
    p_la.add_argument("--score", type=float, default=0.0)
    p_la.add_argument("--section-scores", default="{}")
    p_la.add_argument("--question-type-errors", default="{}")
    p_la.add_argument("--key-errors", default="[]")

    # speaking
    p_speaking = sub.add_parser("speaking", help="Speaking data management")
    p_speaking_sub = p_speaking.add_subparsers(dest="speaking_action")
    p_sa = p_speaking_sub.add_parser("add", help="Save a speaking record")
    p_sa.add_argument("--topic", default="")
    p_sa.add_argument("--part", default="Part 2")
    p_sa.add_argument("--group", default="")
    p_sa.add_argument("--notes", default="")

    # error
    p_error = sub.add_parser("error", help="Error logbook management")
    p_error_sub = p_error.add_subparsers(dest="error_action")
    p_ea = p_error_sub.add_parser("add", help="Add an error tag")
    p_ea.add_argument("--category", required=True, choices=["writing", "reading", "listening", "speaking"])
    p_ea.add_argument("--tag", required=True)
    p_el = p_error_sub.add_parser("list", help="List errors")
    p_el.add_argument("--category", choices=["writing", "reading", "listening", "speaking"])

    # synonym
    p_syn = sub.add_parser("synonym", help="Synonym library management")
    p_syn_sub = p_syn.add_subparsers(dest="synonym_action")
    p_sa2 = p_syn_sub.add_parser("add", help="Add a synonym pair")
    p_sa2.add_argument("--word", required=True)
    p_sa2.add_argument("--synonym", required=True)
    p_sa2.add_argument("--source", default="manual")
    p_sa2.add_argument("--context", default="")
    p_ss = p_syn_sub.add_parser("search", help="Search synonyms")
    p_ss.add_argument("--word", required=True)
    p_syn_sub.add_parser("list", help="List all synonyms")

    # progress
    p_prog = sub.add_parser("progress", help="Progress tracking")
    p_prog_sub = p_prog.add_subparsers(dest="progress_action")
    p_pa = p_prog_sub.add_parser("add", help="Record a score")
    p_pa.add_argument("--skill", required=True, choices=["writing", "reading", "listening", "speaking"])
    p_pa.add_argument("--score", type=float, required=True)
    p_prog_sub.add_parser("show", help="Show progress trends")

    # vocab
    p_vocab = sub.add_parser("vocab", help="Vocabulary management")
    p_vocab_sub = p_vocab.add_subparsers(dest="vocab_action")
    p_va = p_vocab_sub.add_parser("add", help="Add a word")
    p_va.add_argument("--word", required=True)
    p_va.add_argument("--definition", default="")
    p_va.add_argument("--example", default="")
    p_va.add_argument("--synonyms", default="[]")
    p_va.add_argument("--source", default="")
    p_vr = p_vocab_sub.add_parser("review", help="Get words due for review")
    p_vu = p_vocab_sub.add_parser("update", help="Update word after review")
    p_vu.add_argument("--word", required=True)
    p_vu.add_argument("--quality", type=int, required=True, choices=[0, 1, 2, 3, 4, 5])
    p_vl = p_vocab_sub.add_parser("list", help="List vocabulary")
    p_vl.add_argument("--due", action="store_true")
    p_vl.add_argument("--sort-by", default="date", choices=["date", "next_review"])

    # dashboard
    sub.add_parser("dashboard", help="Generate dashboard HTML")

    # backup / restore
    p_backup = sub.add_parser("backup", help="Create backup")
    p_backup.add_argument("--output", default="")
    p_restore = sub.add_parser("restore", help="Restore from backup")
    p_restore.add_argument("--file", required=True)

    # memory
    p_mem = sub.add_parser("memory", help="Coaching memory management")
    p_mem_sub = p_mem.add_subparsers(dest="memory_action")
    p_ma = p_mem_sub.add_parser("add", help="Save a coaching observation")
    p_ma.add_argument("--content", required=True)
    p_ma.add_argument("--category", default="note", choices=["observation", "preference", "weakness", "strength", "strategy", "note"])
    p_ma.add_argument("--skill", default="general", choices=["general", "writing", "reading", "listening", "speaking", "vocab"])
    p_ma.add_argument("--source", default="manual")
    p_ma.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    p_ml = p_mem_sub.add_parser("list", help="List coaching memories")
    p_ml.add_argument("--category", choices=["observation", "preference", "weakness", "strength", "strategy", "note"])
    p_ml.add_argument("--skill", choices=["general", "writing", "reading", "listening", "speaking", "vocab"])
    p_ml.add_argument("--last", type=int, default=0)
    p_ms = p_mem_sub.add_parser("search", help="Search coaching memories")
    p_ms.add_argument("--query", required=True)
    p_md = p_mem_sub.add_parser("delete", help="Delete a coaching memory")
    p_md.add_argument("--id", required=True)

    # status
    sub.add_parser("status", help="Output status bar summary")

    # vault (Obsidian sync)
    p_vault = sub.add_parser("vault", help="Obsidian vault sync")
    p_vault_sub = p_vault.add_subparsers(dest="vault_action")
    p_vault_sub.add_parser("status", help="Show vault path, availability and counts")
    p_ve = p_vault_sub.add_parser("export", help="Export JSON data to the vault as Markdown")
    p_ve.add_argument("--only", default="", help="Comma list: " + ",".join(EXPORT_TYPES))
    p_ve.add_argument("--dry-run", action="store_true")
    p_vi = p_vault_sub.add_parser("import", help="Pull manually-added vault notes back into JSON")
    p_vi.add_argument("--only", default="", help="Comma list: " + ",".join(IMPORT_TYPES))
    p_vi.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    # Route to handler
    handlers = {
        "init": lambda: cmd_init(),
        "config": lambda: _handle_config(args),
        "writing": lambda: _handle_writing(args),
        "reading": lambda: _handle_reading(args),
        "listening": lambda: _handle_listening(args),
        "speaking": lambda: _handle_speaking(args),
        "error": lambda: _handle_error(args),
        "synonym": lambda: _handle_synonym(args),
        "progress": lambda: _handle_progress(args),
        "vocab": lambda: _handle_vocab(args),
        "dashboard": lambda: cmd_dashboard_generate(args),
        "backup": lambda: cmd_backup(args),
        "restore": lambda: cmd_restore(args),
        "status": lambda: cmd_status(args),
        "memory": lambda: _handle_memory(args),
        "vault": lambda: _handle_vault(args),
    }

    handler = handlers.get(args.command)
    if handler:
        return handler()
    else:
        parser.print_help()
        return 1


def _handle_config(args):
    if args.config_action == "get":
        return cmd_config_get()
    elif args.config_action == "set":
        return cmd_config_set(args)
    else:
        print("Usage: ielts_cli.py config [get|set]")
        return 1


def _handle_writing(args):
    if args.writing_action == "add":
        return cmd_writing_add(args)
    elif args.writing_action == "list":
        return cmd_writing_list(args)
    else:
        print("Usage: ielts_cli.py writing [add|list]")
        return 1


def _handle_reading(args):
    if args.reading_action == "add":
        return cmd_reading_add(args)
    else:
        print("Usage: ielts_cli.py reading add ...")
        return 1


def _handle_listening(args):
    if args.listening_action == "add":
        return cmd_listening_add(args)
    else:
        print("Usage: ielts_cli.py listening add ...")
        return 1


def _handle_speaking(args):
    if args.speaking_action == "add":
        return cmd_speaking_add(args)
    else:
        print("Usage: ielts_cli.py speaking add ...")
        return 1


def _handle_error(args):
    if args.error_action == "add":
        return cmd_error_add(args)
    elif args.error_action == "list":
        return cmd_error_list(args)
    else:
        print("Usage: ielts_cli.py error [add|list]")
        return 1


def _handle_synonym(args):
    if args.synonym_action == "add":
        return cmd_synonym_add(args)
    elif args.synonym_action == "search":
        return cmd_synonym_search(args)
    elif args.synonym_action == "list":
        return cmd_synonym_list(args)
    else:
        print("Usage: ielts_cli.py synonym [add|search|list]")
        return 1


def _handle_progress(args):
    if args.progress_action == "add":
        return cmd_progress_add(args)
    elif args.progress_action == "show":
        return cmd_progress_show(args)
    else:
        print("Usage: ielts_cli.py progress [add|show]")
        return 1


def _handle_vocab(args):
    if args.vocab_action == "add":
        return cmd_vocab_add(args)
    elif args.vocab_action == "review":
        return cmd_vocab_review(args)
    elif args.vocab_action == "update":
        return cmd_vocab_update(args)
    elif args.vocab_action == "list":
        return cmd_vocab_list(args)
    else:
        print("Usage: ielts_cli.py vocab [add|review|update|list]")
        return 1


def _handle_memory(args):
    if args.memory_action == "add":
        return cmd_memory_add(args)
    elif args.memory_action == "list":
        return cmd_memory_list(args)
    elif args.memory_action == "search":
        return cmd_memory_search(args)
    elif args.memory_action == "delete":
        return cmd_memory_delete(args)
    else:
        print("Usage: ielts_cli.py memory [add|list|search|delete]")
        return 1


def _handle_vault(args):
    if args.vault_action == "status":
        return cmd_vault_status(args)
    elif args.vault_action == "export":
        return cmd_vault_export(args)
    elif args.vault_action == "import":
        return cmd_vault_import(args)
    else:
        print("Usage: ielts_cli.py vault [status|export|import]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
