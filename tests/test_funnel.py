import json
import shutil
import subprocess
import unittest

from test_notes import html_template


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class FunnelTests(unittest.TestCase):
    def test_funnel_and_export(self):
        script = html_template().split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.split("// 初始化加载", 1)[0]
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = {};
const document = {
  addEventListener() {},
  getElementById(id) { return elements[id] ||= {value:'all', innerHTML:''}; },
  body: {appendChild() {}},
  createElement() { return {click() {}, remove() {}}; }
};
let blob;
const context = vm.createContext({
  document, localStorage:{getItem(){return null;}}, Intl, Date,
  Blob: class {constructor(parts) {blob = parts.join('');}},
  URL:{createObjectURL(){return 'blob:test';}, revokeObjectURL(){}},
  alert() {}
});
vm.runInContext(SCRIPT, context);
vm.runInContext(`
const rows = [
 {company_id:1,country_type:'domestic',client_tier:'A',status:'pending',company_follow_up_date:'2026-10-18'},
 {company_id:2,country_type:'domestic',client_tier:'A',status:'sample_sent',email:'a@b.com',company_follow_up_date:'2026-10-19'},
 {company_id:3,country_type:'domestic',client_tier:'A',status:'pending',prospect_status:'trial'},
 {company_id:4,country_type:'domestic',client_tier:'A',status:'won'},
 {company_id:5,country_type:'domestic',client_tier:'A',status:'won',prospect_status:'lost'},
 {company_id:6,country_type:'overseas',client_tier:'A',status:'won'},
 {company_id:7,country_type:'domestic',client_tier:'B',status:'won'},
 {company_id:8,country_type:'domestic',client_tier:'A',status:'unexpected'}
];
const result = buildFunnelData([...rows, rows[0]], 'domestic', 'A', '2026-10-19');
if (result.total !== 6 || result.active !== 4) throw Error('scope or dedupe');
if (result.counts.team_trial !== 1 || result.counts.won !== 1) throw Error('stage merge');
if (result.groups.lost.length !== 1 || result.groups.unknown.length !== 1) throw Error('lost/unknown');
if (result.groups.overdue.length !== 1 || result.groups.due.length !== 1) throw Error('dates');
if (result.reached.join(',') !== '4,3,2,2,2,1') throw Error('cumulative');
if (buildFunnelData([], 'all', 'all', '2026-10-19').active !== 0) throw Error('empty');
const touch = buildTouchFunnelData([...rows, rows[0], {company_id:9,status:'lost'}]);
if (touch.total !== 9 || touch.counts.won !== 4 || touch.counts.pending !== 2) throw Error('touch counts/dedupe');
if (touch.counts.lost !== 1 || touch.counts.unknown !== 1) throw Error('touch exclusions');
if (touch.reached.join(',') !== '7,5,4,4,4,4') throw Error('touch cumulative');
if (buildTouchFunnelData([{company_id:1,prospect_status:'won'}]).counts.pending !== 1) throw Error('touch must not merge detail stage');
renderTouchFunnel('pipeline-touch-funnel', []);
if (!document.getElementById('pipeline-touch-funnel').innerHTML.includes('当前筛选范围：0')) throw Error('empty touch row');
if (document.getElementById('pipeline-touch-funnel').innerHTML.includes('NaN')) throw Error('empty ratio');
renderTouchFunnel('pipeline-touch-funnel', rows.filter(r=>r.country_type==='domestic' && r.client_tier==='A'));
if (!document.getElementById('pipeline-touch-funnel').innerHTML.includes('当前筛选范围：6')) throw Error('filtered touch row');
updateReportDate(new Date('2026-10-20T16:30:00Z'));
if (document.getElementById('report-date').dateTime !== '2026-10-21') throw Error('Beijing date boundary');
currentData = rows;
renderFunnelView();
showFunnelCustomers('won');
visibleData = [{company_id:1,company_name:'中文,"客户"',company_notes:'首行\\n次行',x_url:'https://x.com/test'}];
exportData();
`, context);
assert(blob.startsWith('\ufeff'));
assert(blob.includes('中文,""客户""'));
assert(blob.includes('"首行\n次行"'));
assert(blob.includes('https://x.com/test'));
assert(blob.includes('\r\n'));
"""
        result = subprocess.run(
            ["node", "-e", harness.replace("SCRIPT", json.dumps(script))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
