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

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression tests")
    def test_notes_ui(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = {};
function element() {
    return {
        value: 'all', style: {}, children: [], handlers: {},
        focus() {}, appendChild(child) { this.children.push(child); },
        addEventListener(type, fn) { this.handlers[type] = fn; },
        querySelector() { return this.preview ||= element(); }
    };
}
const document = {
    addEventListener() {},
    getElementById(id) { return elements[id] ||= element(); },
    createElement() { return element(); }
};
const context = vm.createContext({
    document, localStorage: { getItem() { return null; } },
    console, alerts: [], assert,
    alert(message) { context.alerts.push(message); }
});
vm.runInContext(SCRIPT, context);
vm.runInContext(`
(async () => {
    const original = "客户说：'试用' \\"报价\\"\\\\资料\\n下一行 <标签> & 中文";
    const company = "客户 '名称' \\"双引号\\"";
    currentData = [{company_id: 1, company_name: company, company_notes: original}];
    const tbody = document.getElementById('pipeline-body');
    function preview() {
        tbody.children = [];
        renderTable();
        const row = tbody.children[0];
        const markup = row.innerHTML.match(/<div class="notes-preview"[\\s\\S]*?<\\/div>/)[0];
        assert(!markup.includes('onclick='));
        assert(markup.includes('&lt;标签&gt;'));
        return row.preview;
    }
    preview().handlers.click();
    assert.equal(document.getElementById('notes-input').value, original);
    assert.equal(document.getElementById('notes-modal-title').innerText, '编辑客户情况备注: ' + company);
    closeNotesModal();
    let prevented = false;
    preview().handlers.keydown({key: 'Enter', preventDefault() { prevented = true; }});
    assert(prevented);
    assert.equal(document.getElementById('notes-modal').style.display, 'flex');

    let calls = 0;
    fetch = async (url, options) => {
        calls++;
        const body = JSON.parse(options.body);
        assert.equal(body.company_id, 1);
        currentData[0].company_notes = body.notes;
        return {ok: true, json: async () => ({success: true})};
    };
    fetchPipeline = async () => renderTable();
    for (const notes of ['再次编辑\\n新内容 \\'引号\\'', '']) {
        document.getElementById('notes-input').value = notes;
        await saveCompanyNotes();
        assert.equal(document.getElementById('notes-modal').style.display, 'none');
        assert.equal(document.getElementById('notes-save-btn').disabled, false);
        tbody.children = [];
        renderTable();
        tbody.children[0].preview.handlers.click();
        assert.equal(document.getElementById('notes-input').value, notes);
    }
    assert.equal(calls, 2);
    currentUser = {role: 'observer'};
    tbody.children = [];
    renderTable();
    tbody.children[0].preview.handlers.click();
    assert(document.getElementById('notes-input').readOnly);
    assert(document.getElementById('notes-save-btn').hidden);
    await saveCompanyNotes();
    assert.equal(calls, 2);
    currentUser = {role: 'sales_rep'};
    openNotesModal(1, company, original);
    assert(!document.getElementById('notes-input').readOnly);
    assert(!document.getElementById('notes-save-btn').hidden);
    fetch = async () => ({ok: false, json: async () => ({success: false, error: '测试失败'})});
    await saveCompanyNotes();
    assert.equal(document.getElementById('notes-modal').style.display, 'flex');
    assert.equal(document.getElementById('notes-input').value, original);
    assert.equal(document.getElementById('notes-save-btn').disabled, false);
    assert.equal(alerts.length, 1);
    document.getElementById('notes-save-btn').disabled = true;
    await saveCompanyNotes();
    assert.equal(alerts.length, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
`, context);
"""
        harness = harness.replace("SCRIPT", json.dumps(script))
        result = subprocess.run(
            ["node", "-e", harness], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
