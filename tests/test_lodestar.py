from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lodestar.indexer import LodestarService


class LodestarServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "README.md").write_text("# Demo\n\nRepository overview.\n", encoding="utf-8")
        (self.root / "src" / "app.py").write_text(
            "def login_user(name: str) -> bool:\n    return bool(name)\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "auth.md").write_text(
            "Authentication overview.\nLogin happens in src/app.py.\n",
            encoding="utf-8",
        )
        self.service = LodestarService()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_index_creates_state_and_overview(self) -> None:
        payload = self.service.index(str(self.root))
        self.assertEqual(payload["indexed_files"], 3)
        self.assertTrue((self.root / ".lodestar" / "index.db").exists())

        overview = self.service.overview(str(self.root))
        self.assertIn("Repository", overview["summary"])
        self.assertIn("README.md", overview["key_files"])
        self.assertTrue(overview["subsystems"])

    def test_search_and_retrieve_return_relevant_context(self) -> None:
        self.service.index(str(self.root))
        search = self.service.search(str(self.root), "login auth")
        self.assertTrue(search["results"])
        self.assertTrue(any(item["kind"] in {"function", "file", "section"} for item in search["results"]))

        context = self.service.retrieve(str(self.root), "where is login handled?", budget_tokens=400)
        self.assertTrue(context["code_chunks"])
        self.assertTrue(context["evidence_refs"])
        self.assertTrue(context["symbol_summaries"])

    def test_remember_persists_memory(self) -> None:
        self.service.index(str(self.root))
        remember = self.service.remember(
            str(self.root),
            "auth path",
            "Login logic is in src/app.py",
            evidence_refs=["src/app.py"],
        )
        self.assertGreater(remember["memory_id"], 0)

        context = self.service.retrieve(str(self.root), "auth path", budget_tokens=400)
        self.assertTrue(context["memories"])
        self.assertFalse(context["memories"][0]["stale"])

    def test_memory_becomes_stale_after_evidence_changes(self) -> None:
        self.service.index(str(self.root))
        self.service.remember(
            str(self.root),
            "auth path",
            "Login logic is in src/app.py",
            evidence_refs=["src/app.py"],
        )
        (self.root / "src" / "app.py").write_text(
            "def login_user(name: str) -> bool:\n    return bool(name and name.strip())\n",
            encoding="utf-8",
        )
        self.service.refresh(str(self.root), ["src/app.py"])
        context = self.service.retrieve(str(self.root), "auth path", budget_tokens=500)
        self.assertTrue(context["memories"])
        self.assertTrue(context["memories"][0]["stale"])


    def test_find_usages_detects_used_and_unused_symbols(self) -> None:
        # Create files: one defines a class, another uses it, a third defines an unused interface
        (self.root / "src" / "models.py").write_text(
            "class UserAccount:\n    name: str\n\nclass OrphanedType:\n    pass\n",
            encoding="utf-8",
        )
        (self.root / "src" / "service.py").write_text(
            "from models import UserAccount\n\ndef get_user() -> UserAccount:\n    return UserAccount()\n",
            encoding="utf-8",
        )
        self.service.index(str(self.root))

        # UserAccount is used in service.py
        result = self.service.find_usages(str(self.root), "UserAccount")
        self.assertTrue(result["results"])
        ua = result["results"][0]
        self.assertFalse(ua["is_unused"])
        self.assertGreater(ua["usage_count"], 0)

        # OrphanedType is never referenced outside its definition
        result = self.service.find_usages(str(self.root), "OrphanedType")
        self.assertTrue(result["results"])
        orphan = result["results"][0]
        self.assertTrue(orphan["is_unused"])
        self.assertEqual(orphan["usage_count"], 0)

    def test_find_usages_nonexistent_symbol(self) -> None:
        self.service.index(str(self.root))
        result = self.service.find_usages(str(self.root), "NonExistentSymbol")
        self.assertFalse(result["results"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
