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
    def test_email_queue_ui_has_settings_preview_and_customer_controls(self):
        html = html_template()
        self.assertIn('id="view-email-pool"', html)
        self.assertIn('id="view-email-send"', html)
        self.assertIn('id="view-email-settings"', html)
        self.assertNotIn('<span>✉️</span> <span>邮件线索池</span>', html)
        self.assertIn('onclick="switchEmailPoolMarket(\'domestic\', this)"', html)
        self.assertIn('onclick="switchEmailPoolMarket(\'overseas\', this)"', html)
        self.assertIn('<span>✉️ 邮件发送配置</span>', html)
        self.assertIn('<span>📝 筛选策略</span>', html)
        self.assertIn('<span>🛡️ 组织与权限管理</span>', html)
        self.assertNotIn('id="email-csv-file"', html)
        self.assertIn('id="mail-api-key"', html)
        self.assertIn('id="mail-sending-domain"', html)
        self.assertIn("['provider','sending-domain','from-email','from-name','reply-to','batch-limit','api-key','smtp-host','smtp-port','smtp-security','smtp-username','smtp-password']", html)
        self.assertIn('id="mail-smtp-host"', html)
        self.assertIn('id="mail-auto-enabled"', html)
        self.assertIn("/api/email-automation", html)
        self.assertIn('class="email-config-savebar"', html)
        self.assertIn('id="mail-save-settings"', html)
        self.assertIn("保存只更新配置，不会发送邮件。", html)
        self.assertIn("保存发件配置", html)
        self.assertIn("MAIL_CONFIG_KEY", html)
        self.assertNotIn("addCustomerEmails()", html)
        self.assertNotIn("customer-email-check", html)
        self.assertIn("/api/email-recipients", html)
        self.assertIn('id="feishu-mail-import-panel"', html)
        self.assertIn('id="feishu-mail-sync"', html)
        self.assertIn("/api/feishu-latest-sync/preview", html)
        self.assertIn("/api/feishu-latest-sync/apply", html)
        self.assertIn("✉️ 邮件组线索", html)
        self.assertIn("𝕏 X 线索", html)
        self.assertIn("/api/feishu-latest-sync/preview", html)
        self.assertIn("/api/feishu-latest-sync/apply", html)
        self.assertIn("同步最新数据", html)
        self.assertIn("Discord 社群及 X 推送状态", html)
        self.assertIn("✉️ 邮件组线索", html)
        self.assertIn("𝕏 X 线索", html)
        self.assertNotIn('id="feishu-mail-import-preview"', html)
        self.assertNotIn('id="feishu-mail-import-apply"', html)
        self.assertIn('id="email-status-filter"', html)
        self.assertIn(
            'id="email-status-filter" onchange="renderEmailRecipients()"><option value="">全部</option>',
            html,
        )
        self.assertIn('id="email-market-filter"', html)
        self.assertIn('id="email-batch-sender"', html)
        self.assertIn('id="mail-edit-sender"', html)
        self.assertIn('id="email-package-title"', html)
        self.assertIn('id="email-package-create"', html)
        self.assertIn("/api/email-packages", html)
        self.assertIn("保存不会发送邮件", html)
        self.assertIn('id="package-date-from"', html)
        self.assertIn('id="package-date-to"', html)
        self.assertIn("全选当前筛选", html)
        self.assertIn("发送当前批次", html)
        self.assertIn('id="email-batch-list"', html)
        self.assertIn('id="email-batch-recipient-body"', html)
        self.assertIn("点击批次可查看该次发送涉及的企业与邮箱", html)
        self.assertIn('id="email-replies"', html)
        self.assertIn("/api/email-replies", html)
        self.assertIn("客户邮件回复监控", html)
        self.assertIn("不会把邮箱邮件标为已读", html)
        self.assertIn("emailPushBadge(row.status)", html)
        self.assertIn("✓ 已推送", html)
        pool = html.split('id="view-email-pool"', 1)[1].split('id="view-email-settings"', 1)[0]
        settings = html.split('id="view-email-settings"', 1)[1].split('id="view-permissions"', 1)[0]
        self.assertNotIn('id="mail-api-key"', pool)
        self.assertIn('id="mail-api-key"', settings)
        self.assertIn("{{name}}", html)
        self.assertIn("{{company}}", html)
        self.assertIn("previewEmailPackage(activeMailBatchId)", html)
        self.assertIn("confirmation:`SEND PACKAGE ${id}`", html)
        self.assertNotIn("sendSelectedEmails()", html)
        pool = html.split('id="view-email-pool"', 1)[1].split('id="view-email-send"', 1)[0]
        send = html.split('id="view-email-send"', 1)[1].split('id="view-email-settings"', 1)[0]
        self.assertNotIn('id="email-send-btn"', pool)
        self.assertNotIn('id="email-subject"', pool)
        self.assertIn('id="email-send-btn"', send)
        self.assertIn('id="email-subject"', send)
        self.assertNotIn('id="view-admin-account"', html)
        self.assertNotIn('id="config-admin-account-nav"', html)
        self.assertIn('id="user-edit-modal"', html)
        self.assertIn('id="user-edit-password"', html)
        self.assertIn("/api/user/update", html)
        self.assertIn('class="email-recipient-filters"', html)
        self.assertIn('id="x-outreach-account-heading"', html)
        self.assertIn("isXClosedDmLead", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_single_button_feishu_sync_flow(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
async function run(mode) {
  const elements={}, requests=[];
  let refreshes=0;
  const document={
    addEventListener(){},
    getElementById(id){return elements[id]||={value:'',textContent:'',disabled:false,addEventListener(){}};}
  };
  const context=vm.createContext({document,Intl,Date,console,
    fetch:async(url,options)=>{
      requests.push({url,data:JSON.parse(options.body)});
      if(url.endsWith('/preview')) {
        if(mode==='preview-error') return {ok:false,json:async()=>({error:'读取失败'})};
        return {ok:true,json:async()=>({success:true,
          fingerprint:mode==='missing-fingerprint'?'':'latest-fingerprint',
          mail:{},discord:{},x:{}})};
      }
      if(mode==='stale') return {ok:false,json:async()=>({error:'数据已变化，请重新同步'})};
      return {ok:true,json:async()=>({success:true,mail:{created_companies:0,updated_companies:1,
        created_contacts:0,updated_sent_statuses:1},discord:{create:69,duplicates:0,conflict_count:0},x:{}})};
    },
    refresh:async()=>{
      refreshes++;
      if(mode==='refresh-error') throw Error('refresh failed');
    }
  });
  vm.runInContext(SCRIPT,context);
  vm.runInContext(`currentUser={role:'admin'};
    fetchPipeline=refresh;fetchEmailProspects=refresh;fetchDiscordCommunities=refresh;`,context);
  const first=vm.runInContext('syncLatestFeishuMailData()',context);
  assert.equal(elements['feishu-mail-sync'].disabled,true);
  await vm.runInContext('syncLatestFeishuMailData()',context);
  await first;
  assert.equal(elements['feishu-mail-sync'].disabled,false);
  assert.equal(elements['feishu-mail-sync'].textContent,'同步最新数据');
  if(mode==='preview-error'||mode==='missing-fingerprint') {
    assert.equal(requests.length,1);
    assert.equal(refreshes,0);
    assert.match(elements['feishu-mail-import-status'].textContent,/同步未完成/);
  } else {
    assert.equal(requests.length,2);
    assert.equal(requests[1].url,'/api/feishu-latest-sync/apply');
    assert.equal(requests[1].data.preview_fingerprint,'latest-fingerprint');
    assert.equal(requests[1].data.confirmation,'SYNC FEISHU LATEST DATA');
    if(mode==='stale') {
      assert.equal(refreshes,0);
      assert.match(elements['feishu-mail-import-status'].textContent,/数据已变化/);
    } else {
      assert.match(elements['feishu-mail-import-summary'].textContent,/新增企业 0 家/);
      assert.match(elements['feishu-mail-import-summary'].textContent,/新增 69 个/);
      assert.match(elements['feishu-mail-import-summary'].textContent,/X：更新触达状态 0 条/);
      assert.match(elements['feishu-mail-import-status'].textContent,/同步.*完成/);
      assert.equal(refreshes,mode==='refresh-error'?1:3);
    }
  }
  const before=requests.length;
  vm.runInContext("currentUser={role:'sales_rep'}",context);
  await vm.runInContext('syncLatestFeishuMailData()',context);
  assert.equal(requests.length,before);
}
(async()=>{
  for(const mode of ['success','preview-error','missing-fingerprint','stale','refresh-error']) await run(mode);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_send_batch_detail_and_user_edit_modal_behavior(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const vm=require('node:vm');
const elements={};
const document={
  addEventListener(){},querySelectorAll(){return [];},
  createElement(){return {innerHTML:'',appendChild(){},textContent:'',className:'',type:'',title:''};},
  getElementById(id){
    return elements[id]||={
      value:'',innerHTML:'',textContent:'',innerText:'',style:{},hidden:false,
      classList:{add(){},remove(){},toggle(){},contains(){return false;}},
      reset(){},addEventListener(){},appendChild(){}
    };
  }
};
const requests=[];
const context=vm.createContext({document,Intl,Date,console,fetch:async(url,options)=>{
  requests.push({url,data:JSON.parse(options.body)});
  return {ok:true,json:async()=>({success:true,user:{id:2,username:'edited-rep',
    display_name:'Edited Rep',role:'sales_rep'}})};
}});
vm.runInContext(SCRIPT,context);
vm.runInContext(`
  const check=(condition,message)=>{if(!condition)throw Error(message);};
  mailSenders=[{id:1,from_email:'sales@example.com',configured:true}];
  mailPackages=[{id:7,title:'Draft',status:'draft',sender_id:1,subject:'Hi {{company}}',
    body:'Hello {{name}} at {{email}}',recipient_count:1,
    recipients:[{email:'draft@example.com',company_name:'Draft Co',country_type:'domestic'}]}];
  mailDeliveryBatches=[{batch_id:'delivery-1',title:'Sent batch',status:'sent',recipient_count:1,
    sender:'sales@example.com',recipients:[{email:'sent@example.com',
    company_name:'Sent Co',country_type:'overseas',status:'sent'}]}];
  openEmailBatch('delivery-1');
  check(document.getElementById('email-batch-editor').hidden,'sent batch editor exposed');
  const details=document.getElementById('email-batch-recipient-body').innerHTML;
  check(details.includes('Sent Co')&&details.includes('sent@example.com'),'batch detail lacks company/email');
  openEmailBatch(7);
  check(!document.getElementById('email-batch-editor').hidden,'draft editor hidden');
  check(document.getElementById('email-subject').value==='Hi {{company}}','draft content missing');
  previewEmailPackage(7);
  check(document.getElementById('email-package-preview').innerHTML.includes('draft@example.com'),'preview missing');
`,context);
(async()=>{
  await vm.runInContext(`(async()=>{
    currentUser={id:1,role:'admin',username:'admin'};
    currentUsers=[{id:2,username:'rep',display_name:'Rep',role:'sales_rep',email:'rep@example.com',phone:'123'}];
    openUserEditModal(2);
    check(document.getElementById('user-edit-modal').style.display==='flex','user modal not opened');
    document.getElementById('user-edit-username').value='edited-rep';
    document.getElementById('user-edit-password').value='new-secure-password';
    await saveUserEdit({preventDefault(){}});
    check(document.getElementById('user-edit-modal').style.display==='none','user modal not closed after save');
  })()`,context);
  if(requests.length!==1||requests[0].url!=='/api/user/update'||
      requests[0].data.username!=='edited-rep'||
      requests[0].data.new_password!=='new-secure-password') throw Error('unexpected user update request');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discord_community_dashboard_ui(self):
        html = html_template()
        self.assertIn("Discord 线索", html)
        self.assertIn('onclick="switchOverseasDiscord(this)"', html)
        domestic_nav = html.split('id="domestic-pool-nav"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("Discord", domestic_nav)
        self.assertNotIn('data-channel="x"', domestic_nav)
        self.assertNotIn("X 线索", domestic_nav)
        self.assertIn('id="view-discord"', html)
        self.assertIn('id="discord-community-grid"', html)
        self.assertIn("fetchDiscordCommunities()", html)
        self.assertIn("/api/discord-communities", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_market_channel_navigation_and_email_candidates(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const vm = require('node:vm');
const elements = {};
const document = {
  addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;},
  getElementById(id) {
    return elements[id] ||= {
      value:'', style:{}, textContent:'', innerHTML:'', innerText:'',
      classList:{add(){},remove(){},toggle(){}},
      options:[{value:'all'},{value:'active'}], addEventListener(){},
      replaceChildren(...options){this.options=options;this.value=options[0]?.value;},
      add(option){this.options.push(option);}
    };
  }
};
const context=vm.createContext({document,Intl,Date,console,
  Option:function(text,value){this.text=text;this.value=value;}});
vm.runInContext(SCRIPT,context);
vm.runInContext(`
  const check=(condition,message)=>{if(!condition)throw Error(message);};
  currentData=[
    {company_id:1,country_type:'domestic',email:'cn@example.com'},
    {company_id:2,country_type:'overseas',x_url:'https://x.com/founder',source_channel:'X（用户提供）'},
    {company_id:3,country_type:'overseas',email:'global@example.com',source_channel:'飞书邮件表'},
    {company_id:4,country_type:'overseas'}
  ];
  fetchStats=()=>{};
  renderTable=()=>{visibleData=marketRows();};
  renderSourceCapsules=()=>{};
  switchNav=(nav)=>{globalThis.destination=nav;};
  check(leadMatchesChannel({source_channel:'X（用户提供）'},'x'),'X source classification failed');
  check(!leadMatchesChannel({x_url:'https://x.com/founder',source_channel:'飞书邮件表'},'x'),
    'email-source X homepage was counted as X channel');
  check(leadMatchesChannel({x_url:'https://x.com/founder',email:'both@example.com',
    source_channel:'X（用户提供）'},'email'),'email and X channels must overlap');
  check(leadChannelCounts([
    ...currentData.filter(row=>row.country_type==='domestic'),currentData[0]
  ]).email===1,'duplicate company count');
  currentEmailProspects=[
    {lead_id:1,country_type:'domestic',company_name:'国内示例',email:'cn@example.com',status:'pending'},
    {lead_id:2,country_type:'overseas',company_name:'海外示例',email:'global@example.com',status:'pending'}
  ];
  packageSelectedIds.add(2);
  switchEmailPoolMarket('domestic');
  check(destination==='email-pool' && activeEmailMarket==='domestic','domestic email route');
  check(document.getElementById('email-market-filter').disabled,'market filter not locked');
  const domestic=document.getElementById('email-prospect-body').innerHTML;
  check(domestic.includes('cn@example.com')&&!domestic.includes('global@example.com'),'email markets leaked');
  togglePackageCandidates(true);
  check(packageSelectedIds.size===1&&packageSelectedIds.has(1),'cross-market package selection');
  switchOverseasDiscord();
  check(destination==='discord'&&activeCountry==='overseas','discord market route');
  switchPipelineChannel('overseas','x');
  check(destination==='pipeline'&&activeCountry==='overseas'&&activeLeadChannel==='x','X market route');
  check(leadMatchesChannel(currentData[1],'x')&&!leadMatchesChannel(currentData[2],'x'),
    'overseas X source attribution mixed');
  switchPipelineChannel('overseas','email');
  check(destination==='pipeline'&&activeCountry==='overseas'&&activeLeadChannel==='email',
    'overseas email route');
  check(leadMatchesChannel(currentData[2],'email')&&!leadMatchesChannel(currentData[3],'email'),
    'overseas email attribution mixed');
  switchPipelineMarket('domestic',document.getElementById('domestic-nav'));
  check(!document.getElementById('lead-pool-cards').innerHTML.includes('X 线索'),'domestic X card exposed');
  check(!document.getElementById('lead-pool-cards').innerHTML.includes('Discord'),'domestic Discord card exposed');
  switchPipelineChannel('domestic','x');
  check(activeCountry==='domestic'&&activeLeadChannel==='all','domestic X route was allowed');
`,context);
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_email_navigation_counts_domestic_contacts_and_overseas_channels(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const vm=require('node:vm');
const elements={};
const document={addEventListener(){},getElementById(id){return elements[id]||={textContent:'',hidden:false,
  addEventListener(){},classList:{add(){},remove(){},toggle(){}},innerHTML:''};},querySelectorAll(){return [];}};
const ctx=vm.createContext({document,currentData:[],currentEmailProspects:[
 {lead_id:1,country_type:'domestic'}, {lead_id:2,country_type:'overseas'},
 {lead_id:3,country_type:'overseas'}],currentDiscordCommunities:[],
 discordCommunityTotal:0,activeCountry:'overseas',activeLeadChannel:'email',
 Object,Set});
vm.runInContext(SCRIPT,ctx);
vm.runInContext(`currentEmailProspects=[
 {lead_id:1,country_type:'domestic'}, {lead_id:2,country_type:'overseas'},
 {lead_id:3,country_type:'overseas'}]; activeCountry='overseas';
 currentData=[
  {company_id:3,country_type:'overseas',x_url:'https://x.com/founder',email:'both@example.com',source_channel:'X（用户提供）'},
  {company_id:4,country_type:'overseas',email:'global@example.com',source_channel:'飞书邮件表'},
  {company_id:5,country_type:'overseas',source_channel:'飞书邮件表'}
 ];activeLeadChannel='email';`,ctx);
const countResult=vm.runInContext('emailContactCounts()',ctx);
if(countResult.domestic!==1 || countResult.overseas!==2)
  throw Error('email contact counts must use email contacts');
vm.runInContext('renderLeadPoolNavigation()',ctx);
if(elements['domestic-email-count'].textContent!==1 ||
   elements['overseas-email-count'].textContent!==2 ||
   elements['overseas-x-count'].textContent!=='1 (0)') throw Error('overseas channel attribution');
"""
        result = subprocess.run(["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_market_navigation_isolates_rows_filters_and_stale_stats(self):
        html = html_template()
        self.assertIn('id="domestic-nav"', html)
        self.assertIn('id="overseas-nav"', html)
        self.assertNotIn('id="pending-verification-nav"', html)
        self.assertIn('<li class="nav-item active" id="overseas-nav"', html)
        self.assertIn('<li class="nav-item" id="domestic-nav"', html)
        self.assertIn("let activeCountry = 'overseas';", html)
        self.assertIn('id="pipeline-title">海外客户</h2>', html)
        self.assertIn('id="lead-pool-title">海外客户线索池 · 全部渠道</h3>', html)
        self.assertNotIn('id="pending_verification-x-count"', html)
        self.assertNotIn("pending_verification:'待核验'", html)
        self.assertIn("if (channel === 'email') return Boolean(String(row.email || '').trim());", html)
        self.assertIn("if (channel === 'x') return isXChannelLead(row);", html)
        self.assertIn("function isXChannelLead(row)", html)
        self.assertLess(html.index('id="overseas-nav"'), html.index('id="domestic-nav"'))
        self.assertIn('>推送话术</th>', html)
        self.assertIn('class="company-note-summary"', html)
        self.assertIn('id="data-freshness"', html)
        self.assertIn('数据状态：最新 · 最近更新', html)
        self.assertIn("preview.addEventListener('click', () => openSavedPitch", html)
        self.assertIn("content.textContent = pitchText", html)
        self.assertIn("activeCountry === 'overseas' ? '' : ' <small>家（当前停留）</small>'", html)
        self.assertIn("['海外企业汇总', data.total]", html)
        self.assertIn("['待触达', data.counts.pending]", html)
        self.assertIn("['不触达', data.counts.lost]", html)
        self.assertIn("['已建联', data.counts.contacted]", html)
        self.assertIn("onclick=\"openTierRule('S')\"", html)
        self.assertIn("const TIER_RULES = {", html)
        self.assertIn("content.textContent = rule", html)
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
    activeCountry='domestic';
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
    check((table.match(/scope="col"/g)||[]).length===8,'expected date and seven metric columns');
    check((table.match(/scope="row"/g)||[]).length===5,'expected five date rows');
    check(table.includes('<th scope="col">已推送</th>'),'metrics must be column headers');
    check(table.includes('<th scope="row">2026-10-26</th>'),'dates must be row headers');
    check(table.indexOf('2026-10-26')<table.indexOf('2026-10-22'),'date order incorrect');
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
