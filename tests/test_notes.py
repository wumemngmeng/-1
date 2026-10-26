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

    def test_strategy_archive_uses_compact_rows_and_detail_modal(self):
        html = html_template()
        self.assertIn('id="strategy-detail-modal"', html)
        self.assertIn("openStrategyDetail(", html)
        self.assertIn("查看文本", html)
        self.assertNotIn('style="min-width:420px;white-space:pre-wrap;line-height:1.6">${escapeHtml(row.content)}', html)

    def test_discord_community_dashboard_ui(self):
        html = html_template()
        self.assertIn("Discord 社群看板", html)
        self.assertIn('id="view-discord"', html)
        self.assertIn('id="discord-community-grid"', html)
        self.assertIn("fetchDiscordCommunities()", html)
        self.assertIn("/api/discord-communities", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_market_navigation_isolates_rows_filters_and_stale_stats(self):
        html = html_template()
        self.assertIn('id="domestic-nav"', html)
        self.assertIn('id="overseas-nav"', html)
        self.assertIn("countryFilter.disabled = true", html)
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const vm = require('node:vm');
const elements = {};
const classes = () => ({add(){},remove(){},contains(){return false;}});
const document = {
  addEventListener(){}, querySelectorAll(){return [];},
  getElementById(id) {
    return elements[id] ||= {
      value:'all', style:{}, textContent:'', innerHTML:'', innerText:'',
      classList:classes(), options:[{value:'all'},{value:'active'},{value:'sample_sent'}],
      addEventListener(){},
      replaceChildren(...options){this.options=options;this.value=options[0]?.value;},
      add(option){this.options.push(option);}
    };
  }
};
const pending = [];
const context = vm.createContext({
  document, Intl, Date, console,
  Option:function(text,value){this.text=text;this.value=value;},
  fetch:url=>new Promise(resolve=>pending.push({url,resolve}))
});
vm.runInContext(SCRIPT,context);
(async()=>{
  await vm.runInContext(`(async()=>{
    const check=(condition,message)=>{if(!condition)throw Error(message);};
    currentData=[
      {company_id:1,country_type:'domestic',source_channel:'国内渠道',industry:'国内行业'},
      {company_id:2,country_type:'overseas',source_channel:'海外渠道',industry:'海外行业'}
    ];
    renderTable=()=>{visibleData=marketRows();};
    renderLeadSourceCapsules=()=>{};
    const realFetchStats=fetchStats;
    fetchStats=()=>{};
    refreshMarketOptions();
    document.getElementById('stage-filter').value='sample_sent';
    activeStage='sample_sent';
    selectedSources=new Set(['国内渠道']);
    switchPipelineMarket('overseas',document.getElementById('overseas-nav'));
    check(activeCountry==='overseas','wrong market');
    check(visibleData.length===1 && visibleData[0].company_id===2,'domestic row leaked');
    check(activeStage==='all' && selectedSources.size===0,'filters leaked');
    check(currentSourceChannels.join(',')==='海外渠道','source choices leaked');
    check(document.getElementById('industry-filter').options.length===2,'industry choices leaked');
    resetFilters();
    check(activeCountry==='overseas','reset unlocked market');
    switchPipelineMarket('domestic',document.getElementById('domestic-nav'));
    check(visibleData[0].company_id===1,'overseas row leaked');
    check(activeStage==='sample_sent' && selectedSources.has('国内渠道'),'filters not restored');
    renderRecentOutreachTable([
      {date:'2026-10-26',sample_sent:3,contacted:2},
      {date:'2026-10-25',won:1},
      {date:'2026-10-24'},
      {date:'2026-10-23'},
      {date:'2026-10-22'},
      {date:'2026-10-21'}
    ]);
    const table=document.getElementById('yesterday-outreach-stats').innerHTML;
    check(table.includes('<table') && table.includes('查看更多'),'recent table missing');
    check((table.match(/scope="col"/g)||[]).length===6,'expected five date columns');
    check(table.indexOf('2026-10-22')<table.indexOf('2026-10-26'),'date order incorrect');
    check(!table.includes('2026-10-21'),'more than five days shown');
    check(table.includes('<td>5</td>'),'daily total incorrect');
    let destination;
    switchNav=(nav,elem)=>{destination=nav;};
    activeCountry='overseas';
    openOutreachDetails();
    check(destination==='funnel','more link destination incorrect');
    check(document.getElementById('funnel-country').value==='overseas','more link lost market');
    check(document.getElementById('funnel-tier').value==='all','more link retained stale tier');
    activeCountry='domestic';
    const waiting=realFetchStats();
    activeCountry='overseas';
    globalThis.waiting=waiting;
  })()`,context);
  if(pending[0].url!=='/api/stats?country=domestic')throw Error('unscoped stats');
  pending[0].resolve({json:async()=>({total_companies:999})});
  await context.waiting;
  if(elements['stat-companies']?.innerText===999)throw Error('stale stats leaked');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
