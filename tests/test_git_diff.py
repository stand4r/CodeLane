"""Unified diff parsing and hunk-level staging."""
import shutil
import subprocess

import pytest

from core.git_diff import build_patch, diff_stats, parse_diff, parse_shortstat


SIMPLE_DIFF = """diff --git a/demo.txt b/demo.txt
index 1111111..2222222 100644
--- a/demo.txt
+++ b/demo.txt
@@ -1,3 +1,4 @@
 line1
-line2
+line2 changed
+line2.5
 line3
@@ -10,3 +11,3 @@ def section():
 ctx
-old tail
+new tail
 ctx
"""


def test_parse_diff_splits_files_and_hunks():
    files = parse_diff(SIMPLE_DIFF)
    assert len(files) == 1
    file = files[0]
    assert file.path == "demo.txt"
    assert len(file.hunks) == 2
    first, second = file.hunks
    assert (first.added, first.removed) == (2, 1)
    assert (second.added, second.removed) == (1, 1)
    assert first.old_start == 1 and first.new_start == 1
    assert "def section()" in second.summary()
    assert file.summary() == "+3 −2"


def test_parse_diff_handles_several_files_and_untracked_style():
    text = SIMPLE_DIFF + """diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+alpha
+beta
"""
    files = parse_diff(text)
    assert [file.path for file in files] == ["demo.txt", "new.txt"]
    assert files[1].old_path == "/dev/null"
    assert files[1].added() == 2 and files[1].removed() == 0


def test_build_patch_keeps_only_selected_hunks():
    file = parse_diff(SIMPLE_DIFF)[0]
    patch = build_patch(file, [1])
    assert "--- a/demo.txt" in patch
    assert "+new tail" in patch
    assert "line2 changed" not in patch
    assert patch.endswith("\n")
    assert build_patch(file, []) == ""


def test_diff_stats_ignores_headers():
    assert diff_stats(SIMPLE_DIFF) == (3, 2)


def test_parse_shortstat():
    text = " 3 files changed, 12 insertions(+), 4 deletions(-)\n"
    assert parse_shortstat(text) == (3, 12, 4)
    assert parse_shortstat(" 1 file changed, 1 insertion(+)\n") == (1, 1, 0)
    assert parse_shortstat("") == (0, 0, 0)


@pytest.mark.skipif(shutil.which("git") is None, reason="git недоступен")
def test_selected_hunk_is_staged_in_real_repository(tmp_path):
    def git(*args):
        # git.exe occasionally dies with 0xC0000005 on this machine (AV/EDR),
        # so retry the process instead of failing the whole check.
        for attempt in range(5):
            done = subprocess.run(["git", *args], cwd=tmp_path, text=True,
                                  capture_output=True, encoding="utf-8")
            if done.returncode == 0:
                return done.stdout
            if done.returncode not in (0xC0000005, 0xC0000409, 0xC000013A):
                break
        assert done.returncode == 0, (done.stdout, done.stderr)
        return done.stdout

    git("init", "-q")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")
    target = tmp_path / "demo.txt"
    lines = [f"line {number}" for number in range(1, 21)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    git("add", "demo.txt")
    git("commit", "-qm", "init")
    lines[1] = "line 2 changed"
    lines[18] = "line 19 changed"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    file = parse_diff(git("diff", "--", "demo.txt"))[0]
    assert len(file.hunks) == 2
    patch_file = tmp_path / "selected.patch"
    patch_file.write_bytes(build_patch(file, [0]).encode("utf-8"))
    git("apply", "--cached", str(patch_file))

    staged = git("diff", "--cached", "--", "demo.txt")
    assert "line 2 changed" in staged
    assert "line 19 changed" not in staged
    remaining = git("diff", "--", "demo.txt")
    assert "line 19 changed" in remaining
    assert "line 2 changed" not in remaining


