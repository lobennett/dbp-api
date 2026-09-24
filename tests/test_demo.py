import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
