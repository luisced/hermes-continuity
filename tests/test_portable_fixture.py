"""Portable test fixtures must use the configured temporary directory."""
import ast
import unittest
from pathlib import Path


class PortableFixtureTests(unittest.TestCase):
    def test_general_workflow_fixture_does_not_pin_a_developer_directory(self):
        path = Path(__file__).with_name('test_general_followthrough.py')
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'TemporaryDirectory':
                self.assertFalse(any(k.arg == 'dir' and isinstance(k.value, ast.Constant)
                                     and isinstance(k.value.value, str) and Path(k.value.value).is_absolute()
                                     for k in node.keywords), 'test fixture pins an absolute developer directory')
