"""Regression tests for #7240 — model dropdown truncates colon-tagged model names.

Custom-provider model IDs can carry tag/variant colons inside the model name
(``@custom:omni:kg/stepfun/step-3.7-flash:free``, ``@custom:omni:ollamacloud/qwen3.5:397b``).
The ``@custom:`` branch of ``getModelLabel()`` (static/ui.js) split ``rest`` at
the LAST colon and rendered only the suffix (``free``, ``397b``), so the picker's
primary label (``.model-opt-name``) truncated the model name.

The fix mirrors the backend grammar in ``_parse_provider_qualified_model_id()``
(api/config.py): the model tail after the provider slug is preserved verbatim;
only an endpoint-style ``host:port`` slug keeps the last colon as provider
plumbing. The parity test below asserts the frontend display and the backend
route-parser agree on the same id.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.config import _parse_provider_qualified_model_id  # noqa: E402

# (model_id, expected display label). The reported #7240 shapes first, then the
# sibling shapes the fix must not regress (untagged custom models, single-segment
# custom ids, host:port slugs, named provider + tagged model).
CUSTOM_LABEL_CASES = [
    # --- the reported truncations -----------------------------------------
    ("@custom:omni:kg/stepfun/step-3.7-flash:free", "kg/stepfun/step-3.7-flash:free"),
    ("@custom:omni:kg/tencent/hy3:free", "kg/tencent/hy3:free"),
    ("@custom:omni:ollamacloud/gemma4:31b", "ollamacloud/gemma4:31b"),
    ("@custom:omni:ollamacloud/qwen3.5:397b", "ollamacloud/qwen3.5:397b"),
    # --- untagged custom models keep working ------------------------------
    ("@custom:omni:gemini/gemini-3.1-flash-lite", "gemini/gemini-3.1-flash-lite"),
    ("@custom:omni:kg/meituan/longcat-2.0-free", "kg/meituan/longcat-2.0-free"),
    ("@custom:ai_gateway:Qwen3.6-35B-A3B", "Qwen3.6-35B-A3B"),
    ("@custom:qwen397b-64k", "qwen397b-64k"),
    # --- host:port slugs must not regress into "port:model" ---------------
    ("@custom:192.168.1.5:11434:llama4", "llama4"),
    ("@custom:localhost:1234:qwen3", "qwen3"),
    # --- named provider + tagged model (grammar sibling) ------------------
    ("@custom:mykey:model-a:free", "model-a:free"),
    ("@custom:backup:model-a", "model-a"),
    # Ambiguous shape (non-dotted slug + numeric segment): treated as a
    # tagged model, mirroring the backend grammar.
    ("@custom:gw:8080:free", "8080:free"),
]

# Non-@custom colon-tagged ids already rendered correctly and must stay so.
# Note: `@openrouter:meta/llama-4:free` labels as `llama-4:free` — the
# deliberate #3360 rule strips only the first slash-segment; what #7240 cares
# about is that the `:free` tag itself survives the colon handling.
GENERIC_CASES = [
    ("@ollama:qwen3.8:27b-mtp-q8_0", "qwen3.8:27b-mtp-q8_0"),
    ("@openrouter:meta/llama-4:free", "llama-4:free"),
]


def _js_model_labels(model_ids: list[str]) -> dict[str, str]:
    """Drive the REAL ``getModelLabel()`` from static/ui.js under Node.

    Same mechanics as tests/test_dotted_model_label.py: only SINKS are stubbed
    (``_dynamicModelLabels`` starts empty, ``_fmtOllamaLabel`` is identity);
    every decision function is the shipped source, eval'd — so drift fails here
    instead of rendering wrong in the picker.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", _GET_MODEL_LABEL_DRIVER, str(REPO_ROOT / "static" / "ui.js"),
         json.dumps(model_ids)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


# Boundary-slicing approach proven in tests/test_dotted_model_label.py: regex
# literals inside getModelLabel() defeat a naive brace counter, so bound the
# function by the next top-level function instead.
_GET_MODEL_LABEL_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
const start = ui.indexOf('function getModelLabel(');
if (start < 0) throw new Error('getModelLabel not found');
const after = ui.indexOf('\nfunction _gatewayProviderName(', start);
if (after < 0) throw new Error('getModelLabel end boundary not found');
// Sinks only: no catalog has been fetched yet, so dynamic labels are empty.
const _dynamicModelLabels = {};
function _fmtOllamaLabel(s){ return s; }
// The dotted normalizer and the Set it closes over sit just above
// getModelLabel(); without them the eval'd function ReferenceErrors.
const _stripStart = ui.indexOf('const _BEDROCK_REGION_PREFIXES');
if (_stripStart < 0 || _stripStart > start) throw new Error('strip block not found');
eval(ui.slice(_stripStart, start));
eval(ui.slice(start, after));
const out = {};
for (const m of JSON.parse(process.argv[2])) out[m] = getModelLabel(m);
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.parametrize(
    "model_id,expected", CUSTOM_LABEL_CASES + GENERIC_CASES
)
def test_model_label_preserves_colon_tagged_model(model_id, expected):
    """The picker's primary label (``.model-opt-name``) shows the full name."""
    assert _js_model_labels([model_id])[model_id] == expected


def test_frontend_label_matches_backend_grammar_for_custom_ids():
    """Display and route parsing must agree on the same ``@custom:`` id.

    ``getModelLabel()`` is the pre-catalog-fetch display fallback; the backend
    ``_parse_provider_qualified_model_id()`` is the authority for what a
    qualified id means. If they diverge, the picker shows a different model
    name than the one that would actually be routed.
    """
    js_labels = _js_model_labels([c[0] for c in CUSTOM_LABEL_CASES])
    for model_id, _ in CUSTOM_LABEL_CASES:
        parsed = _parse_provider_qualified_model_id(model_id)
        assert parsed is not None, model_id
        bare_model, provider = parsed
        assert js_labels[model_id] == bare_model, (
            f"display {js_labels[model_id]!r} != backend model {bare_model!r} "
            f"(provider {provider!r}) for {model_id!r}"
        )
