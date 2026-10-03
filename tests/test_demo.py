import ast
import json
from pathlib import Path
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_basic_creation_and_reopening(self):
        notebook = json.loads((ROOT / "examples/dbp_api_demo.ipynb").read_text())
        source = next("".join(cell["source"]) for cell in notebook["cells"]
                      if cell["id"] == "create")
        client = Mock()
        client.create_experiment.return_value.assign.return_value.experiment_id = "saved-demo"
        context = dict(client=client, experiment_id="new", filters=[],
                       content_query="", print=Mock())
        exec(source, context)
        client.create_experiment.return_value.assign.assert_called_once_with(
            subjects=2, items_per_subject=5)
        self.assertEqual(context["experiment_id"], "saved-demo")
        exec(source, context)
        client.assignments.assert_called_once_with("saved-demo")
        client.create_experiment.assert_called_once()

    def test_optional_examples_do_not_run(self):
        notebook = json.loads((ROOT / "examples/dbp_api_demo.ipynb").read_text())
        for cell_id in ("tracking", "optional-foils"):
            cell = next(cell for cell in notebook["cells"] if cell["id"] == cell_id)
            self.assertEqual(cell["cell_type"], "markdown")

    def test_single_clean_notebook(self):
        notebooks = list((ROOT / "examples").glob("*.ipynb"))
        self.assertEqual([path.name for path in notebooks], ["dbp_api_demo.ipynb"])
        notebook = json.loads(notebooks[0].read_text())
        self.assertEqual(notebook["nbformat"], 4)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])
                ast.parse("".join(cell["source"]))

    def test_api_only_source(self):
        self.assertFalse((ROOT / "src" / "dbp_pgl_runner" / "__init__.py").exists())
        self.assertFalse((ROOT / "launch-pilot.command").exists())

    def test_login_has_only_a_password_prompt(self):
        notebook = json.loads((ROOT / "examples/dbp_api_demo.ipynb").read_text())
        login = next("".join(cell["source"]) for cell in notebook["cells"]
                     if cell["cell_type"] == "code" and "client.login(" in "".join(cell["source"]))
        self.assertNotIn("input(", login)
        self.assertIn('website_url = "http://127.0.0.1:8773"', login)
        self.assertIn('getpass("Website password: ")', login)


if __name__ == "__main__":
    unittest.main()
