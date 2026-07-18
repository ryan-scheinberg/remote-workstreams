"""Drift alarms for the static PWA in remote_workstreams/web/.

No backend or browser: assert the files exist, index.html wires them up, the JS
speaks remote_workstreams/protocol.py literally (message types, states, audio formats),
and every file parses (node --check, when node is installed).
"""

import json
import re
import shutil
import subprocess
import typing
from pathlib import Path

import pytest

from remote_workstreams import protocol

WEB = Path(__file__).resolve().parent.parent / "remote_workstreams" / "web"
REQUIRED = [
    "index.html",
    "styles.css",
    "app.js",
    "ui.js",
    "audio.js",
    "pairing.js",
    "audio-worklet.js",
    "manifest.webmanifest",
    "icon.svg",
]
JS_FILES = [name for name in REQUIRED if name.endswith(".js")]
WORKLET_JS = {"audio-worklet.js"}  # plain scripts; the rest are ES modules


def _js() -> str:
    return "\n".join((WEB / name).read_text() for name in JS_FILES)


def _message_types(union) -> set[str]:
    inner = typing.get_args(union)[0]  # the Union inside Annotated
    return {
        typing.get_args(model.model_fields["type"].annotation)[0]
        for model in typing.get_args(inner)
    }


CLIENT_TYPES = _message_types(protocol.ClientMessage)
SERVER_TYPES = _message_types(protocol.ServerMessage)
PIPELINE_STATES = set(typing.get_args(protocol.State.model_fields["state"].annotation))


def test_required_files_exist():
    missing = [name for name in REQUIRED if not (WEB / name).is_file()]
    assert not missing, f"missing from remote_workstreams/web/: {missing}"


def test_index_references_assets():
    html = (WEB / "index.html").read_text()
    for ref in ["app.js", "styles.css", "manifest.webmanifest", "icon.svg"]:
        assert ref in html, f"index.html does not reference {ref}"


def test_worklet_loaded_by_name():
    assert "audio-worklet.js" in _js(), "no JS loads audio-worklet.js"


def test_every_ui_element_id_exists_in_index():
    ids = set(re.findall(r'\$\("([\w-]+)"\)', (WEB / "ui.js").read_text()))
    html = (WEB / "index.html").read_text()
    missing = [i for i in sorted(ids) if f'id="{i}"' not in html]
    assert not missing, f"ui.js looks up ids missing from index.html: {missing}"


def test_every_protocol_literal_appears_in_js():
    js = _js()
    for literal in CLIENT_TYPES | SERVER_TYPES | PIPELINE_STATES:
        assert f'"{literal}"' in js, f'protocol literal "{literal}" never appears in the JS'


def test_js_sends_exactly_the_client_message_types():
    sent = set(re.findall(r'\btype:\s*"(\w+)"', _js()))
    assert sent == CLIENT_TYPES, f"JS constructs {sent}, protocol defines {CLIENT_TYPES}"


def test_app_dispatch_handles_exactly_the_server_message_types():
    handled = set(re.findall(r'case "(\w+)":', (WEB / "app.js").read_text()))
    assert handled == SERVER_TYPES, f"app.js handles {handled}, protocol defines {SERVER_TYPES}"


def test_takeover_stops_the_stale_tab_reconnect_loop():
    js = (WEB / "app.js").read_text()
    assert "reconnectBlocked" in js
    assert 'msg.message === "another connection took over"' in js
    assert "Reload this tab to take it back" in js


def test_audio_formats_match_protocol():
    js = _js()
    assert f'"{protocol.MIC_FORMAT.encoding}"' in js
    assert str(protocol.MIC_FORMAT.sample_rate) in js, "mic default rate missing from JS"
    assert str(protocol.TTS_FORMAT.sample_rate) in js, "TTS default rate missing from JS"


def test_manifest_is_a_standalone_pwa_with_an_icon():
    manifest = json.loads((WEB / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["icons"], "manifest declares no icons"
    for icon in manifest["icons"]:
        assert (WEB / icon["src"]).is_file(), f"manifest icon {icon['src']} missing"


@pytest.mark.parametrize("name", JS_FILES)
def test_js_parses(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; JS syntax unchecked on this machine")
    path = WEB / name
    if name in WORKLET_JS:
        proc = subprocess.run([node, "--check", str(path)], capture_output=True)
    else:  # ES module: --check alone assumes CommonJS, so feed it as module input
        proc = subprocess.run(
            [node, "--check", "--input-type=module", "-"],
            input=path.read_bytes(),
            capture_output=True,
        )
    assert proc.returncode == 0, f"{name}: {proc.stderr.decode()}"
