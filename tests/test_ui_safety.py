"""The web UI must not turn untrusted text (RFP content, model output, server messages) into script."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.main import app

from .test_authorization_api import A

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def run_node(script: str) -> object:
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, cwd=STATIC)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestEscapingHelpers:
    def esc(self, text: str) -> str:
        return run_node(f"console.log(JSON.stringify(require('./safe.js').esc({json.dumps(text)})))")

    def safe_url(self, url) -> str:
        return run_node(f"console.log(JSON.stringify(require('./safe.js').safeUrl({json.dumps(url)})))")

    def test_html_metacharacters_are_escaped_including_quotes(self):
        assert self.esc("""<img src=x onerror="alert('1')">&""") == (
            "&lt;img src=x onerror=&quot;alert(&#39;1&#39;)&quot;&gt;&amp;"
        )

    def test_an_attribute_value_cannot_be_broken_out_of(self):
        assert '"' not in self.esc('" onmouseover="x') and "'" not in self.esc("' onmouseover='x")

    def test_missing_values_become_empty_text(self):
        assert run_node("console.log(JSON.stringify([require('./safe.js').esc(null), require('./safe.js').esc(undefined)]))") == ["", ""]

    def test_numbers_are_converted(self):
        assert run_node("console.log(JSON.stringify(require('./safe.js').esc(42)))") == "42"

    @pytest.mark.parametrize(
        "url", ["javascript:alert(1)", " javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>", "vbscript:x", "file:///etc/passwd"]
    )
    def test_script_and_other_non_web_urls_are_rejected(self, url):
        assert self.safe_url(url) == ""

    @pytest.mark.parametrize("url", ["https://learn.microsoft.com/azure", "http://kb.example/doc.pdf"])
    def test_web_urls_are_kept(self, url):
        assert self.safe_url(url).startswith(url.split("/")[0])

    def test_relative_urls_resolve_against_the_page_origin(self):
        assert run_node("console.log(JSON.stringify(require('./safe.js').safeUrl('/outputs/a', 'https://app.example')))") == (
            "https://app.example/outputs/a"
        )


class TestPageStructure:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_the_page_has_no_inline_scripts_or_event_handlers(self):
        assert not re.search(r"<script(?![^>]*\bsrc=)", self.html), "inline <script> would be blocked by the CSP"
        assert not re.search(r"\son[a-z]+\s*=", self.html), "inline event handler attributes"
        assert "javascript:" not in self.html

    def test_generated_markup_has_no_inline_handlers_either(self):
        assert not re.search(r"\son[a-z]+\s*=", self.js)
        assert not re.search(r"\b(eval|document\.write)\s*\(|new Function\(", self.js)

    def test_the_scripts_load_from_static(self):
        assert re.findall(r'<script src="([^"]+)"', self.html) == ["/static/safe.js", "/static/app.js"]

    def test_links_built_from_model_output_go_through_safe_url(self):
        # evidence links come from the model: the href must be the sanitised URL
        assert "safeUrl(e.source_url)" in self.js

    def test_dynamic_values_inserted_as_html_are_escaped(self):
        # every string concatenated into innerHTML next to user/model content uses esc() or a numeric helper
        for needle in ("esc(req.text)", "esc(resp.answer_markdown", "esc(section.narrative_markdown", "esc(doc.filename)"):
            assert needle in self.js, needle


@pytest.fixture
async def http():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client


class TestSecurityHeaders:
    async def test_the_ui_is_served_with_a_content_security_policy(self, http):
        response = await http.get("/")
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
        assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
        assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert response.headers["x-frame-options"] == "DENY"

    async def test_static_assets_get_the_policy_too(self, http):
        response = await http.get("/static/app.js")
        assert response.status_code == 200
        assert "content-security-policy" in response.headers

    async def test_api_responses_are_not_sniffable(self, http):
        response = await http.get("/health")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" not in response.headers  # /docs needs its own inline scripts

    async def test_error_responses_carry_the_headers_too(self, client):
        response = await client.post(
            "/draft/stream", json={"rfp_id": "missing", "client_id": "a", "project_id": "b"}, headers=A
        )
        assert response.status_code == 404
        assert response.headers["x-content-type-options"] == "nosniff"
