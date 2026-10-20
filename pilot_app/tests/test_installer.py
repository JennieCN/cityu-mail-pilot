# -*- coding: utf-8 -*-
"""Tests for the installer's lifecycle: install → uninstall → reinstall.

These are *static* assertions about `deploy_pilot.sh` and the drill script. They
exist because the interesting failures here are invisible in code review and
invisible on the machine that has been running the software for months:

* the installer never created its own service account. It was forked from the
  older single-user deploy script, which did create it, and every machine it ran
  on had inherited that account -- so `install -d -o cityumail` always worked.
  On a machine that has never run the old script (anybody following the README
  from a fresh clone) it fails with "install: invalid user" and the ERR trap
  aborts the install. Meanwhile preflight happily checks that `useradd` exists.
* `--uninstall` removed the systemd units but left the nginx vhost behind, so
  the machine kept serving a host whose backend was gone (`nginx -t` still
  passes, so nothing complains).
* `--purge` deleted the service account, which made it a one-way door: without
  the account nothing above can be installed again.

The real proof is `tools/uninstall_drill.sh` on a disposable machine (CI job
"真卸一次"). These tests are the cheap half: they fail in two seconds, on every
push, when one of those properties is edited away.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "pilot_app" / "deploy_pilot.sh"
DRILL = ROOT / "tools" / "uninstall_drill.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "uninstall-drill.yml"


class ServiceAccountTests(unittest.TestCase):
    def setUp(self):
        self.text = INSTALLER.read_text(encoding="utf-8")

    def test_the_installer_creates_its_own_service_account(self):
        self.assertIn("ensure_service_user", self.text)
        self.assertIn("useradd", self.text)
        self.assertIn("groupadd", self.text)

    def test_creating_the_account_is_idempotent(self):
        """Re-running the installer is the normal case (every upgrade)."""
        block = self.text.split("ensure_service_user()", 1)[1].split("\n}", 1)[0]
        self.assertIn("getent group cityumail", block)
        self.assertIn("id -u cityumail", block)
        # ...and both creations are guarded by those checks, not attempted blindly.
        self.assertLess(block.index("getent group cityumail"), block.index("groupadd"))
        self.assertLess(block.index("id -u cityumail"), block.index("useradd"))

    def test_it_runs_before_anything_needs_the_account(self):
        main = self.text.split("# --------------------------------------------------------------------- main", 1)[1]
        self.assertIn("ensure_service_user", main)
        self.assertLess(main.index("ensure_service_user"), main.index("do_install_files"))

    def test_the_group_is_named_explicitly(self):
        """Whether `useradd --system` invents a same-named group depends on
        USERGROUPS_ENAB in /etc/login.defs, and the units say Group=cityumail."""
        self.assertIn("--gid cityumail", self.text)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.block = INSTALLER.read_text(encoding="utf-8").split("do_uninstall()", 1)[1].split("\n}", 1)[0]

    def test_it_removes_the_nginx_site(self):
        self.assertIn("/etc/nginx/sites-enabled/cityu-mail-pilot", self.block)
        self.assertIn("/etc/nginx/sites-available/cityu-mail-pilot", self.block)

    def test_it_reloads_nginx_only_when_it_is_running(self):
        self.assertIn("systemctl is-active --quiet nginx", self.block)
        self.assertIn("systemctl reload nginx", self.block)

    def test_it_keeps_config_and_data_without_purge(self):
        after_units = self.block.split("if [[ \"$PURGE\" == 1 ]]", 1)[0]
        self.assertNotIn("rm -rf \"$CONFIG_DIR\"", after_units)
        self.assertNotIn("rm -rf \"$DATA_DIR\"", after_units)

    def test_purge_removes_the_group_too(self):
        """userdel without groupdel leaves a group nobody owns; and the installer
        then has to cope with it on the next install."""
        purge = self.block.split("if [[ \"$PURGE\" == 1 ]]", 1)[1]
        self.assertIn("userdel cityumail", purge)
        self.assertIn("groupdel cityumail", purge)

    def test_purge_says_what_it_deliberately_kept(self):
        """Certificates are not deleted -- deleting them is irreversible and
        Let's Encrypt rate-limits re-issue -- so the least it can do is say so."""
        purge = self.block.split("if [[ \"$PURGE\" == 1 ]]", 1)[1]
        self.assertIn("刻意没删", purge)
        self.assertIn("letsencrypt", purge)


class NoTrailingGuardTests(unittest.TestCase):
    """A function whose last line is `[[ ... ]] && something` returns 1 whenever
    the test is false.

    That is not a style point: under `set -e` it aborts the caller. The installer
    died this way on every *fresh* install that passed `--admin-email` -- the
    documented happy path -- right after writing pilot.env, before installing a
    single unit. It was invisible for months because every machine running the
    installer either already had pilot.env (the function returns early) or had
    been set up by the older single-user script. A drill on a clean machine found
    it on its first run.

    `||` chains are allowed: `public_ip` ends with one on purpose, and its callers
    handle the failure. The flagged shape is a *guard* -- a line that begins with
    a test.
    """

    def test_no_function_ends_with_a_conditional_guard(self):
        lines = INSTALLER.read_text(encoding="utf-8").splitlines()
        offenders = []
        start = None
        for index, line in enumerate(lines):
            if re.match(r"^[a-z_]+\\(\\) \\{", line):
                start = index
            elif line == "}" and start is not None:
                body = [item for item in lines[start + 1:index]
                        if item.strip() and not item.strip().startswith("#")]
                if body:
                    last = body[-1].strip()
                    if last.startswith("[[") or last.startswith("[ "):
                        if "&&" in last or "||" in last:
                            offenders.append(f"{lines[start].strip()} → {last}")
                start = None
        self.assertEqual(offenders, [], f"这些函数的最后一行是条件守卫，会让整个脚本提前退出：{offenders}")


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.text = DRILL.read_text(encoding="utf-8")

    def test_the_drill_refuses_to_run_on_an_installed_machine(self):
        """It purges. A script that can delete a production database must not
        look like something you run casually."""
        self.assertIn("--i-know-this-wipes-this-machine", self.text)
        self.assertIn("/etc/cityu-mail-pilot/pilot.env", self.text)

    def test_it_checks_the_things_reading_the_code_cannot_answer(self):
        """Each of these is one of the three bugs this drill was written for."""
        # 1. the account is recreated after purge (otherwise: one-way door)
        self.assertIn("服务账号被重新创建", self.text)
        self.assertIn("purge 之后还能装", self.text)
        # 2. the nginx vhost is actually gone, not merely "the units are gone"
        self.assertIn("nginx 站点（enabled）", self.text)
        # 3. a reinstall reuses the database file instead of rebuilding it
        self.assertIn("同一个数据库文件", self.text)
        # 4. a wrong purge confirmation deletes nothing
        self.assertIn("输错确认词时非零退出", self.text)

    def test_it_is_run_from_a_published_workflow(self):
        self.assertTrue(WORKFLOW.is_file(), f"缺少 {WORKFLOW}")
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("tools/uninstall_drill.sh", workflow)
        # A drill that only ever runs when someone remembers to click is a drill
        # that stops being true. Weekly, plus on demand.
        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)

    def test_the_workflow_is_published(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools import publish_export as export

        selected = {str(path) for path in export.iter_files()}
        self.assertIn("tools/uninstall_drill.sh", selected)
        self.assertIn(".github/workflows/uninstall-drill.yml", selected)

    def test_the_drill_does_not_touch_the_live_machine(self):
        """It is the one script here that deletes things, so it must never be
        able to reach production: no ssh, no remote host, no secrets."""
        for forbidden in ("ssh ", "scp ", "secrets.", "119."):
            self.assertNotIn(forbidden, self.text, f"演练脚本里出现了 {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
