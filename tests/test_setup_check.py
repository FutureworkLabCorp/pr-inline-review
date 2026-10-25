"""Dependency checks run without GitHub access or application services."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_check


class SkillDependencyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.skill = self.root / "pr-inline-review"
        self.skill.mkdir()

    def install(self, skill, *files):
        directory = self.root / skill
        directory.mkdir(exist_ok=True)
        for filename in files:
            (directory / filename).write_text("fixture\n")

    def check_dependencies(self):
        output = StringIO()
        with redirect_stdout(output):
            ready = setup_check.check_skill_dependencies(self.skill)
        return ready, output.getvalue()

    def test_complete_installation_is_ready(self):
        self.install("code-review-sweep", "sweep.py", "finding-gate.md")
        self.install("review-rediagnose", "SKILL.md")
        ready, output = self.check_dependencies()
        self.assertTrue(ready)
        self.assertNotIn("[WARN]", output)
        self.assertNotIn("[FAIL]", output)

    def test_missing_rediagnosis_warns_without_blocking_ordinary_review(self):
        self.install("code-review-sweep", "sweep.py", "finding-gate.md")
        ready, output = self.check_dependencies()
        self.assertTrue(ready)
        self.assertIn("[WARN]", output)
        self.assertIn(str(self.root / "review-rediagnose" / "SKILL.md"), output)
        self.assertNotIn("[FAIL]", output)

    def test_missing_required_engine_or_gate_blocks_review(self):
        for missing, present in (("sweep.py", "finding-gate.md"),
                                 ("finding-gate.md", "sweep.py")):
            with self.subTest(missing=missing):
                self.install("code-review-sweep", present)
                ready, output = self.check_dependencies()
                self.assertFalse(ready)
                self.assertIn("[FAIL]", output)
                self.assertIn(missing, output)
                (self.root / "code-review-sweep" / present).unlink()


if __name__ == "__main__":
    unittest.main()
