"""Drift alarms for the plugin wrappers.

The Codex plugin carries a real COPY of the deploy skill — codex's plugin cache
copier drops symlinks, so a link would install as an empty directory (verified
on codex-cli 0.142.5). This pins the copy to the source; edit skills/deploy and
re-copy when it fails. The Claude plugin keeps symlinks (Claude follows them).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "skills" / "deploy-rw"
CODEX_COPY = REPO / "plugins" / "codex" / "skills" / "deploy-rw"


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_codex_plugin_deploy_skill_matches_the_source():
    assert _files(CODEX_COPY) == _files(SOURCE), (
        "plugins/codex/skills/deploy-rw drifted from skills/deploy-rw —"
        " re-copy it (cp -R skills/deploy-rw plugins/codex/skills/)"
    )


def test_codex_plugin_scripts_stay_executable():
    for script in (CODEX_COPY / "scripts").iterdir():
        assert script.stat().st_mode & 0o111, f"{script.name} lost its exec bit"


def test_claude_plugin_skills_are_symlinks_into_skills():
    skills_dir = REPO / "plugins" / "claude-code" / "skills"
    for link in skills_dir.iterdir():
        assert link.is_symlink(), f"{link.name} should be a symlink"
        assert (skills_dir / link.readlink()).resolve() == (REPO / "skills" / link.name)
