"""Admin grouping retains every original panel and introduces no network path."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"


class AdminWorkspaceTests(unittest.TestCase):
    def test_all_admin_panels_are_mapped_once(self):
        html = (STATIC / "index.html").read_text()
        admin = html.split('<section id="section-admin"', 1)[1]
        panels = re.findall(r'<details class="panel" id="([^"]+)"', admin)
        js = (STATIC / "app.js").read_text()
        registry = js.split('const ADMIN_VIEWS = [', 1)[1].split('\n];', 1)[0]
        mapped = re.findall(r"'(panel-[^']+)'", registry)
        self.assertCountEqual(mapped, panels)
        self.assertEqual(len(mapped), len(set(mapped)))

    def test_existing_attention_shortcut_reveals_panel(self):
        js = (STATIC / "app.js").read_text()
        self.assertIn('revealAdminPanel(item.panel);\n      panel.open = true;', js)

    @unittest.skipUnless(shutil.which('node'), 'Node needed for isolated JS behaviour check')
    def test_health_numbers_are_separate_from_complete_explanations(self):
        js = (STATIC / 'app.js').read_text()
        block = 'function adminHealthMetricRows' + js.split('function adminHealthMetricRows', 1)[1].split('function renderAdminHealth', 1)[0]
        checks = r"""
const assert=require('node:assert/strict');
function humanDuration(n){return `${n} 秒`}
const rows=adminHealthMetricRows({users:97,max_users:300,active_users:92,paused_users:5,
pending_messages:12,failed_reports:4,failed_reports_per_mail:3,failed_reports_digests:1,failed_reports_created_24h:2,
mailboxes_polled_recently:28,mailboxes:30,healthy_mailboxes:26,mailboxes_paused:2,
newest_poll_seconds:90,working:{ok:24},school_mail_24h:8,
mailboxes_with_school_mail_24h:4,school_mail_7d:99});
assert.equal(rows.length,8);
rows.forEach(([label,value,note])=>{assert.match(value,/^\d+( \/ \d+)?$/);assert.ok(label&&note)});
assert.equal(rows[4][1],'4');assert.match(rows[4][2],/逐封邮件 3，每日简报 1/);
assert.equal(rows[4][0],'累计失败记录');
assert.match(rows[4][2],/最近24小时创建且仍失败 2 份/);
assert.match(rows[4][2],/不等于当前故障数/);
assert.match(rows[5][2],/含取信失败/);
assert.match(rows[6][2],/2 个已暂停/);assert.match(rows[6][2],/90 秒前/);
assert.match(rows[6][2],/多 2 位/);assert.match(rows[6][2],/还没转发/);
assert.equal(rows[7][1],'8');assert.match(rows[7][2],/来自 4 个邮箱 · 7 天 99 封/);
const empty=adminHealthMetricRows({});assert.equal(empty[7][1],'0');
assert.doesNotMatch(empty[6][2],/undefined前|多 .* 位/);
"""
        result = subprocess.run([shutil.which('node'), '-e', block + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node needed for isolated JS behaviour check')
    def test_view_switching_and_polling(self):
        js = (STATIC / "app.js").read_text()
        block = 'const ADMIN_VIEWS = [' + js.split('const ADMIN_VIEWS = [', 1)[1].split('/* ---- 「需要你处理」', 1)[0]
        harness = r"""
const assert = require('node:assert/strict');
const nodes = new Map(); let metricsStops=0, modelStops=0;
const window = { scrollTo() {} };
function $(id) { if (!nodes.has(id)) nodes.set(id, {
  tagName: id.startsWith('panel-') ? 'DETAILS' : 'DIV', open:false,
  classList: { hidden:false, toggle(_, hidden){this.hidden=hidden}, contains(){return this.hidden} },
  querySelectorAll(){return []}, textContent:''
}); return nodes.get(id); }
function panelIsOpen(id){return $(id).open}
function stopMetrics(){metricsStops++}
function stopModelServer(){modelStops++}
"""
        checks = r"""
selectAdminView('overview');
assert.equal($('admin-health').classList.hidden,false);
assert.equal($('panel-users').classList.hidden,true);
selectAdminView('users');
assert.equal($('panel-users').classList.hidden,false);
assert.equal($('admin-health').classList.hidden,true);
selectAdminView('models'); $('panel-model-server').open=true;
selectAdminView('users');
assert.equal($('panel-model-server').open,false);
assert.ok(modelStops>0); assert.ok(metricsStops>0);
revealAdminPanel('panel-mail');
assert.equal(adminView,'mail');
assert.equal($('panel-mail').classList.hidden,false);
selectAdminView('all');
ADMIN_VIEWS.forEach(v=>v.nodes.forEach(id=>assert.equal($(id).classList.hidden,false)));
selectAdminView('unknown'); assert.equal(adminView,'all');
assert.equal(ADMIN_VIEWS.some(v=>v.nodes.includes('panel-users')),true);
"""
        result = subprocess.run([shutil.which('node'), '-e', harness + block + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
