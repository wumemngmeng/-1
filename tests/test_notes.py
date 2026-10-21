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
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_prospect_edit_and_save_states(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = {};
const document = {
  addEventListener() {},
  querySelectorAll() {return [];},
  getElementById(id) {
    return elements[id] ||= {
      value:'', style:{}, textContent:'', className:'', disabled:false,
      addEventListener() {},
      classList:{contains(name) {return elements[id].className.split(' ').includes(name);}}
    };
  }
};
const context = vm.createContext({
  document, localStorage:{getItem(){return null;}}, Intl, Date, console,
  fetch:async()=>({ok:true,json:async()=>({success:true})})
});
vm.runInContext(SCRIPT, context);
(async()=>{
  await vm.runInContext(`(async()=>{
    currentUser={role:'admin',display_name:'管理员'};
    currentData=[{company_id:1,company_name:'测试客户',company_notes:'原备注',industry:'测试'}];
    fetchPipeline=async()=>{};
    openProspectDetails(1);
    if (!document.getElementById('prospect-save-btn').disabled) throw Error('clean save enabled');
    document.getElementById('p-notes').value='已修改';
    updateProspectEditState();
    if (document.getElementById('prospect-save-btn').disabled) throw Error('dirty save disabled');
    resetProspectForm();
    if (document.getElementById('p-notes').value!=='原备注') throw Error('reset failed');
    setProspectOwner('测试负责人');
    if (document.getElementById('prospect-save-btn').disabled) throw Error('owner change not dirty');
    document.getElementById('p-status').value='lost';
    await saveProspectDetails({preventDefault(){}});
    if (document.getElementById('p-message').className!=='error') throw Error('missing validation');
    document.getElementById('p-status').value='pending';
    await saveProspectDetails({preventDefault(){}});
    if (document.getElementById('p-message').className!=='success') throw Error('missing success');
    if (!document.getElementById('prospect-save-btn').disabled) throw Error('saved form still dirty');
    currentUser={role:'observer'};
    openProspectDetails(1);
    document.getElementById('p-notes').value='不应保存';
    updateProspectEditState();
    if (!document.getElementById('prospect-save-btn').disabled) throw Error('observer save enabled');
  })()`, context);
  assert.equal(elements['prospect-save-btn'].textContent,'保存编辑');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertIn('<body class="auth-checking">', html)
        self.assertIn("正在恢复登录状态", html)
        self.assertIn("initializeApp();", html)
        self.assertNotIn("localStorage.getItem('aibubu_current_user')", html)
        self.assertNotIn("默认: admin / 密码: admin123", html)


if __name__ == "__main__":
    unittest.main()
