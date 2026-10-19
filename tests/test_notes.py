import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.database import DatabaseManager


def html_template():
    tree = ast.parse((ROOT / "web/server.py").read_text())
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "HTML_TEMPLATE"
                for target in node.targets)
    )


class NotesTests(unittest.TestCase):
    def test_notes_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "test.db"))
            company_id = db.insert_company({"name": "测试客户"})
            for notes in ["首行\n客户说：'试用' \"报价\" C:\\资料 <标签> & 中文", "再次编辑", ""]:
                self.assertTrue(db.update_company_notes(company_id, notes))
                row = next(r for r in db.query_pipeline() if r["company_id"] == company_id)
                self.assertEqual(row["company_notes"], notes)

    def test_notes_ui(self):
        html = html_template()
        table = html.split("<!-- 数据总表 -->", 1)[1].split("<!-- 视窗 2", 1)[0]
        details = html.split('<div id="prospect-modal"', 1)[1]
        self.assertNotIn("职位/头衔", table)
        self.assertNotIn("客户情况备注", table)
        self.assertNotIn("建联入口", table)
        self.assertIn('id="p-notes"', details)
        self.assertIn("客户情况与备注", details)
        self.assertIn("const prospectFields = {notes:'notes'", html)
        self.assertIn("const value = key === 'notes' ? r.company_notes : r[key]", html)


if __name__ == "__main__":
    unittest.main()
