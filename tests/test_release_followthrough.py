"""Release contracts must ship together with the reusable engine."""
import unittest
from pathlib import Path
from scripts.build_release import source_files


class FollowthroughReleaseTests(unittest.TestCase):
    def test_general_workflow_documentation_and_quality_example_are_packaged(self):
        root = Path(__file__).resolve().parents[1]
        files = {p.relative_to(root).as_posix() for p in source_files(root)}
        self.assertTrue({
            'continuity/followthrough.py',
            'continuity/quality.py',
            'docs/GENERAL-FOLLOWTHROUGH.md',
            'docs/CONFIGURATION.md',
            'docs/DEVELOPMENT.md',
            'docs/examples/wellbeing-quality.json',
        }.issubset(files), 'release omits the general workflow contract or example')
