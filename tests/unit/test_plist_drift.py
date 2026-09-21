"""Installed-vs-template plist drift tests (spec WD-14, ADR 0010, LAG-684).

Drift is confirmed live: the installed `com.alex.transcriber` plist diverged
from the committed template and stopped routing through `preflight.sh`, which
is exactly what ADR 0010 relies on. The watchdog cannot heal a plist it never
looks at, so it now hash-compares the two on every cycle and reports drift as
a warning.

Warning, never verdict: a drifted plist is a deployment fact, not a liveness
fact. Letting it flip the verdict would make the watchdog kickstart a daemon
that is running perfectly well (HC-4/HC-5 lesson, one level up). And the whole
check is read-only, so --dry-run needs no special case (HC-8/HC-9).
"""

import plistlib
import subprocess
from pathlib import Path

import pytest

import health_check

PROJECT_DIR = Path(__file__).parents[2]
COMMITTED_TEMPLATE = PROJECT_DIR / "docs" / "launchd" / "com.alex.transcriber.plist.template"

MINIMAL_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- a comment the installed copy will not carry -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.alex.transcriber</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-c</string>
        <string>exec '__REPO__/preflight.sh'</string>
    </array>
    <key>WorkingDirectory</key>
    <string>__REPO__</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>__HOME__</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""

REPO = "/Users/harald/Code/superwhisper-obsidian-wf"
HOME = "/Users/harald"


def render(text=MINIMAL_TEMPLATE, *, repo=REPO, home=HOME):
    return health_check.render_plist_template(text, repo=repo, home=home)


@pytest.fixture
def template_file(tmp_path):
    path = tmp_path / "com.alex.transcriber.plist.template"
    path.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
    return path


@pytest.fixture
def installed_file(tmp_path):
    path = tmp_path / "com.alex.transcriber.plist"
    path.write_text(render(), encoding="utf-8")
    return path


class TestRenderPlistTemplate:
    def test_substitutes_repo_and_home_placeholders(self):
        rendered = render()
        assert "__REPO__" not in rendered
        assert "__HOME__" not in rendered
        assert f"{REPO}/preflight.sh" in rendered

    def test_rendered_template_is_a_valid_plist(self):
        parsed = plistlib.loads(render().encode("utf-8"))
        assert parsed["Label"] == "com.alex.transcriber"
        assert parsed["EnvironmentVariables"]["HOME"] == HOME

    def test_committed_template_renders_without_placeholders(self):
        rendered = render(COMMITTED_TEMPLATE.read_text(encoding="utf-8"))
        assert "__REPO__" not in rendered
        assert "__HOME__" not in rendered
        plistlib.loads(rendered.encode("utf-8"))


class TestPlistFingerprint:
    def test_ignores_comments_and_whitespace(self):
        """Semantics decide, not formatting — otherwise every reflow is 'drift'."""
        rendered = render()
        reflowed = plistlib.dumps(plistlib.loads(rendered.encode("utf-8"))).decode("utf-8")
        assert reflowed != rendered
        assert health_check.plist_fingerprint(reflowed) == health_check.plist_fingerprint(rendered)

    def test_key_order_does_not_change_the_hash(self):
        parsed = plistlib.loads(render().encode("utf-8"))
        reordered = dict(reversed(list(parsed.items())))
        assert health_check.plist_fingerprint(
            plistlib.dumps(reordered).decode("utf-8")
        ) == health_check.plist_fingerprint(render())

    def test_changed_value_changes_the_hash(self):
        drifted = render().replace("preflight.sh", "auto_transcribe.py")
        assert health_check.plist_fingerprint(drifted) != health_check.plist_fingerprint(render())

    def test_malformed_plist_returns_none(self):
        assert health_check.plist_fingerprint("not a plist at all") is None


class TestCheckPlistDrift:
    def test_identical_plists_are_clean(self, installed_file, template_file):
        result = health_check.check_plist_drift(installed_file, template_file, repo=REPO, home=HOME)
        assert result["status"] == "clean"
        assert result["installed_hash"] == result["template_hash"]
        assert result["preflight_routed"] is True

    def test_changed_program_arguments_are_drift(self, tmp_path, template_file):
        installed = tmp_path / "installed.plist"
        installed.write_text(render().replace("preflight.sh", "auto_transcribe.py"), encoding="utf-8")
        result = health_check.check_plist_drift(installed, template_file, repo=REPO, home=HOME)
        assert result["status"] == "drift"
        assert result["installed_hash"] != result["template_hash"]

    def test_reports_when_installed_plist_does_not_route_through_preflight(self, tmp_path, template_file):
        """The live ADR 0010 violation LAG-684 was filed for."""
        installed = tmp_path / "installed.plist"
        installed.write_text(render().replace("exec '__REPO__/preflight.sh'".replace("__REPO__", REPO), "exec true"))
        result = health_check.check_plist_drift(installed, template_file, repo=REPO, home=HOME)
        assert result["preflight_routed"] is False

    def test_missing_installed_plist_is_reported_not_raised(self, tmp_path, template_file):
        result = health_check.check_plist_drift(tmp_path / "absent.plist", template_file, repo=REPO, home=HOME)
        assert result["status"] == "installed_missing"
        assert result["installed_hash"] is None

    def test_missing_template_is_reported_not_raised(self, tmp_path, installed_file):
        result = health_check.check_plist_drift(installed_file, tmp_path / "absent.template", repo=REPO, home=HOME)
        assert result["status"] == "template_missing"

    def test_unparsable_installed_plist_is_reported_not_raised(self, tmp_path, template_file):
        installed = tmp_path / "installed.plist"
        installed.write_text("<plist><dict><key>truncated", encoding="utf-8")
        result = health_check.check_plist_drift(installed, template_file, repo=REPO, home=HOME)
        assert result["status"] == "unreadable"

    def test_accepts_str_paths(self, installed_file, template_file):
        result = health_check.check_plist_drift(str(installed_file), str(template_file), repo=REPO, home=HOME)
        assert result["status"] == "clean"

    def test_never_writes_anything(self, installed_file, template_file, monkeypatch):
        """HC-8: the drift check is read-only, which is why --dry-run needs no branch."""

        def forbidden(*_args, **_kwargs):
            raise AssertionError("drift check must not write or spawn processes")

        monkeypatch.setattr(Path, "write_text", forbidden)
        monkeypatch.setattr(Path, "write_bytes", forbidden)
        monkeypatch.setattr(subprocess, "run", forbidden)
        before = installed_file.read_text(encoding="utf-8")
        health_check.check_plist_drift(installed_file, template_file, repo=REPO, home=HOME)
        assert installed_file.read_text(encoding="utf-8") == before


class TestCommittedTemplateIsSelfConsistent:
    def test_committed_template_compares_clean_against_its_own_render(self, tmp_path):
        installed = tmp_path / "com.alex.transcriber.plist"
        installed.write_text(render(COMMITTED_TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
        result = health_check.check_plist_drift(installed, COMMITTED_TEMPLATE, repo=REPO, home=HOME)
        assert result["status"] == "clean"
        assert result["preflight_routed"] is True


class TestReporting:
    def test_drift_is_a_warning_not_a_verdict(self, capsys):
        report = health_check.assess_health(
            {"schema": health_check.HEARTBEAT_SCHEMA_VERSION, "phase": "scanning", "writer": "daemon"},
            10.0,
            {"pid": 8078, "last_exit_status": 0},
            300.0,
        )
        report["plist_drift"] = {
            "status": "drift",
            "installed_hash": "aaaaaaaa",
            "template_hash": "bbbbbbbb",
            "preflight_routed": False,
            "detail": None,
        }
        health_check.print_report(report)
        out = capsys.readouterr().out
        assert report["verdict"] == "healthy"
        assert "verdict: healthy" in out
        assert "drift" in out.lower()
        assert "preflight.sh" in out

    def test_clean_drift_check_prints_no_warning(self, capsys):
        report = health_check.assess_health(
            {"schema": health_check.HEARTBEAT_SCHEMA_VERSION, "phase": "scanning", "writer": "daemon"},
            10.0,
            {"pid": 8078, "last_exit_status": 0},
            300.0,
        )
        report["plist_drift"] = {
            "status": "clean",
            "installed_hash": "aaaaaaaa",
            "template_hash": "aaaaaaaa",
            "preflight_routed": True,
            "detail": None,
        }
        health_check.print_report(report)
        assert "drift" not in capsys.readouterr().out.lower()

    def test_report_without_drift_key_still_prints(self, capsys):
        """Callers that never pass --plist must not trip over a missing key."""
        report = health_check.assess_health(None, None, None, 300.0)
        health_check.print_report(report)
        assert "verdict: unhealthy" in capsys.readouterr().out


class TestCli:
    def _heartbeat(self, tmp_path):
        import json
        import time

        path = tmp_path / "hb.json"
        path.write_text(
            json.dumps(
                {
                    "schema": health_check.HEARTBEAT_SCHEMA_VERSION,
                    "phase": "scanning",
                    "writer": "daemon",
                    "cycle": 1,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_drift_warning_does_not_change_exit_code(self, tmp_path, monkeypatch, capsys):
        heartbeat = self._heartbeat(tmp_path)
        installed = tmp_path / "installed.plist"
        installed.write_text(render().replace("preflight.sh", "auto_transcribe.py"), encoding="utf-8")
        template = tmp_path / "tpl.template"
        template.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t-15\tcom.alex.transcriber\n")
        rc = health_check.main(
            [
                "--heartbeat",
                str(heartbeat),
                "--plist",
                str(installed),
                "--plist-template",
                str(template),
                "--repo",
                REPO,
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "drift" in out.lower()

    def test_no_plist_argument_means_no_drift_check(self, tmp_path, monkeypatch, capsys):
        heartbeat = self._heartbeat(tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t-15\tcom.alex.transcriber\n")
        rc = health_check.main(["--heartbeat", str(heartbeat), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "drift" not in out.lower()

    def test_default_template_path_points_at_the_committed_template(self):
        assert Path(health_check.DEFAULT_PLIST_TEMPLATE).name == COMMITTED_TEMPLATE.name
