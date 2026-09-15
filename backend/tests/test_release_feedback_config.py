"""
Release-gate coverage for the Windows build script `desktop/scripts/build-release.py`.

Why this lives in the backend suite: `python -m pytest backend/tests/` is the documented
regression command, and the feedback configuration gate is a release blocker. Putting the
test anywhere else would mean the gate is not actually exercised by the normal run.

The regression this file exists for: the bundle gate originally accepted only `"rag"` /
`'rag'`, but the real Vite/esbuild output inlines the value as a **backtick** template
literal (`` r.appId=`rag` ``). That mismatch would have failed a real release build.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "desktop" / "scripts" / "build-release.py"


def _load_build_release():
    spec = importlib.util.spec_from_file_location("build_release_under_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_release():
    assert BUILD_SCRIPT.is_file(), f"missing release script: {BUILD_SCRIPT}"
    return _load_build_release()


def _make_dist(files: dict[str, str]) -> Path:
    """Creates a throwaway frontend/dist tree with the given assets."""
    root = Path(tempfile.mkdtemp()) / "dist"
    assets = root / "assets"
    assets.mkdir(parents=True)
    for name, text in files.items():
        (assets / name).write_text(text, encoding="utf-8")
    return root


def test_canonical_release_constants(build_release):
    """The pinned release values must be exactly the ones the plan requires."""
    assert build_release.FEEDBACK_API_BASE == "https://feedback.xn--fhqths51enha.cn"
    assert build_release.FEEDBACK_APP_ID == "rag"
    # Backticks must be accepted: that is the form the real minifier emits.
    assert "`" in build_release.FEEDBACK_BUNDLE_APP_ID_QUOTES


@pytest.mark.parametrize(
    "api_base, app_id",
    [
        ("", "rag"),
        (None, "rag"),
        ("   ", "rag"),
        ("https://feedback.xn--fhqths51enha.cn", ""),
        ("https://feedback.xn--fhqths51enha.cn", None),
        ("http://feedback.xn--fhqths51enha.cn", "rag"),
        ("feedback.xn--fhqths51enha.cn", "rag"),
        ("/api", "rag"),
        ("https://feedback.xn--fhqths51enha.cn?tenant=rag", "rag"),
        ("https://feedback.xn--fhqths51enha.cn#top", "rag"),
        ("https://feedback.xn--fhqths51enha.cn/", "rag"),
        ("https://staging.example.com", "rag"),
        ("https://feedback.xn--fhqths51enha.cn", "not-rag"),
        ("https://feedback.xn--fhqths51enha.cn", "rag-web"),
        ("https://feedback.xn--fhqths51enha.cn", "RAG"),
        ("https://feedback.xn--fhqths51enha.cn /v1", "rag"),
    ],
)
def test_validate_feedback_config_rejects_bad_input(build_release, api_base, app_id):
    with pytest.raises(build_release.FeedbackConfigError):
        build_release.validate_feedback_config(
            api_base, app_id, expected_api_base=build_release.FEEDBACK_API_BASE
        )


def test_validate_feedback_config_normalizes_surrounding_whitespace(build_release):
    """
    Surrounding whitespace (a common env/YAML artifact) is stripped before validation,
    so `" rag "` is accepted while the appId identity check itself stays exact.
    """
    build_release.validate_feedback_config(
        f"  {build_release.FEEDBACK_API_BASE}  ",
        "  rag  ",
        expected_api_base=build_release.FEEDBACK_API_BASE,
    )


def test_validate_feedback_config_accepts_canonical_pair(build_release):
    build_release.validate_feedback_config(
        build_release.FEEDBACK_API_BASE,
        build_release.FEEDBACK_APP_ID,
        expected_api_base=build_release.FEEDBACK_API_BASE,
    )


def test_feedback_build_env_injects_and_detects_drift(build_release):
    env = build_release.feedback_build_env({})
    assert env["VITE_FEEDBACK_API_BASE"] == build_release.FEEDBACK_API_BASE
    assert env["VITE_FEEDBACK_APP_ID"] == build_release.FEEDBACK_APP_ID

    matching = build_release.feedback_build_env(
        {"VITE_FEEDBACK_API_BASE": build_release.FEEDBACK_API_BASE, "VITE_FEEDBACK_APP_ID": "rag"}
    )
    assert matching["VITE_FEEDBACK_APP_ID"] == "rag"

    # Ambient drift must fail loudly instead of being silently overridden or honoured.
    with pytest.raises(build_release.FeedbackConfigError):
        build_release.feedback_build_env({"VITE_FEEDBACK_API_BASE": "http://localhost:5173"})
    with pytest.raises(build_release.FeedbackConfigError):
        build_release.feedback_build_env({"VITE_FEEDBACK_APP_ID": "web"})


@pytest.mark.parametrize(
    "bundle_text",
    [
        # The exact shape observed in the real Vite/esbuild production bundle.
        "r.apiBase=`https://feedback.xn--fhqths51enha.cn`,r.appId=`rag`",
        'r.apiBase="https://feedback.xn--fhqths51enha.cn",r.appId="rag"',
        "r.apiBase='https://feedback.xn--fhqths51enha.cn',r.appId='rag'",
        "apiBase: https://feedback.xn--fhqths51enha.cn appId=rag",
    ],
)
def test_bundle_gate_accepts_every_real_inlining_form(build_release, bundle_text):
    dist = _make_dist({"index.js": bundle_text})
    try:
        hits = build_release.verify_feedback_bundle_contains_config(dist)
        assert [p.name for p in hits] == ["index.js"]
    finally:
        shutil.rmtree(dist.parent, ignore_errors=True)


def test_bundle_gate_rejects_missing_configuration(build_release):
    cases = [
        {"index.js": "r.appId=`rag`"},  # API base never injected
        {"index.js": "https://feedback.xn--fhqths51enha.cn storage fragment drag"},  # no appId token
        {"other.txt": "https://feedback.xn--fhqths51enha.cn rag"},  # no JS assets at all
    ]
    for files in cases:
        dist = _make_dist(files)
        try:
            with pytest.raises(build_release.FeedbackConfigError):
                build_release.verify_feedback_bundle_contains_config(dist)
        finally:
            shutil.rmtree(dist.parent, ignore_errors=True)


def test_bundle_gate_requires_host_and_app_id_in_the_same_chunk(build_release):
    """Host in one chunk and appId in another is not proof that both were injected."""
    dist = _make_dist(
        {
            "a.js": "apiBase=`https://feedback.xn--fhqths51enha.cn`",
            "b.js": "appId=`rag`",
        }
    )
    try:
        with pytest.raises(build_release.FeedbackConfigError):
            build_release.verify_feedback_bundle_contains_config(dist)
    finally:
        shutil.rmtree(dist.parent, ignore_errors=True)


def test_app_id_word_matcher_does_not_match_inside_other_words(build_release):
    """`rag` inside storage/fragment/drag must not be mistaken for the appId."""
    pattern = re.compile(r"(?<![A-Za-z0-9_-])rag(?![A-Za-z0-9_-])")
    assert pattern.search("storage fragment drag") is None
    assert pattern.search("appId=rag") is not None
    assert pattern.search("`rag`") is not None
