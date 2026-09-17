import ast
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


class ExampleTests(unittest.TestCase):
    def test_notebook_is_valid_output_free_python_and_uses_same_runner(self):
        path = Path(__file__).resolve().parents[1] / "examples/digital_brain_pilot.ipynb"
        notebook = json.loads(path.read_text())
        self.assertEqual(notebook["nbformat"], 4)
        runner = Mock()
        scope = {}
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
                ast.parse("".join(cell["source"]))
        with (patch("dbp_pgl_runner.runner.StudyRunner", return_value=runner),
              patch("builtins.input", return_value="http://localhost:8000"),
              patch("getpass.getpass", return_value="synthetic-code")):
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    exec(compile("".join(cell["source"]), str(path), "exec"), scope)
        self.assertEqual([call[0] for call in runner.mock_calls],
                         ["connect", "prepare", "status", "run", "sync", "status"])
        self.assertTrue(runner.run.call_args.kwargs["integration_test"])
        self.assertEqual(runner.run.call_args.args, ("s001",))
