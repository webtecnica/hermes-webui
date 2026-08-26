"""#6946 re-gate: a real option from provider A must never retire a synthetic,
routable selection owned by provider B (cross-provider orphan dedup)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(src: str, name: str) -> str:
    import re
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name} not found"
    start = match.start()
    i = src.index("{", match.end())
    depth = 1
    i += 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _run_harness(tmp_path, case: str) -> dict:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    parts = [
        _function(src, "_getOptionProviderId"),
        _function(src, "_modelPickerOptionIdentity"),
        _function(src, "_modelPickerCanonicalIdentity"),
        _function(src, "_providerQualifiedPresetRest"),
        _function(src, "_deduplicateModelPickerOptions"),
    ]
    driver = tmp_path / f"driver_{case}.js"
    driver.write_text(
        "\n".join(parts)
        + r"""
class Node {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.parentElement=null; this.value=''; }
  appendChild(child) { child.parentElement=this; this.children.push(child); return child; }
  removeChild(child) { this.children=this.children.filter(item=>item!==child); child.parentElement=null; }
  querySelectorAll(selector) {
    if(selector==='optgroup') return this.children.filter(child=>child.tagName==='OPTGROUP');
    return [];
  }
  get options() {
    return this.tagName==='SELECT'
      ? this.children.flatMap(child=>child.tagName==='OPTGROUP'?child.children:[child])
      : undefined;
  }
}
globalThis.window={_activeProvider:null};
globalThis.document={createElement:tag=>new Node(tag)};
globalThis._dynamicModelLabels={};
globalThis._modelStateForSelect=()=>({model:'',model_provider:null});
globalThis._applyModelToDropdown=()=>null;
globalThis.S={session:null};
function makeSelect() {
  const select=new Node('select');
  const groupA=new Node('optgroup'); groupA.dataset.provider='provider-a'; select.appendChild(groupA);
  const real=new Node('option'); real.value='shared-model'; real.textContent='shared-model'; groupA.appendChild(real);
  const orphan=new Node('option'); orphan.value='@provider-b:shared-model'; orphan.dataset.custom='1'; select.appendChild(orphan);
  return select;
}
const select=makeSelect();
const removed=_deduplicateModelPickerOptions(select,select.value);
const groups=select.querySelectorAll('optgroup').map(item=>item.children.map(option=>option.value));
const orphans=select.children.filter(child=>child.tagName==='OPTION').map(option=>option.value);
process.stdout.write(JSON.stringify({removed,groups,orphans}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cross_provider_orphan_not_retired_by_other_provider_twin(tmp_path):
    """Provider A's real `shared-model` option must NOT retire provider B's
    synthetic `@provider-b:shared-model` orphan (#6946 re-gate)."""
    out = _run_harness(tmp_path, "cross")
    assert out["orphans"] == ["@provider-b:shared-model"], (
        "orphan owned by provider B must survive dedup (provider mismatch): "
        + str(out)
    )
    assert out["groups"] == [["shared-model"]], str(out)
