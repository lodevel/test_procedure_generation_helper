"""Unit tests for the LLM-useless frontmatter stripper in tab_context."""

from workflow_editor.llm.tab_context import _strip_llm_useless_frontmatter


def test_no_frontmatter_returns_unchanged():
    body = "# A doc\n\nNo frontmatter here.\n"
    assert _strip_llm_useless_frontmatter(body) == body


def test_unterminated_frontmatter_returns_unchanged():
    body = "---\nfield: value\n# never closed\n"
    assert _strip_llm_useless_frontmatter(body) == body


def test_strips_related_list():
    src = (
        "---\n"
        "doc_id: foo\n"
        "title: Foo\n"
        "related:\n"
        "  - bar\n"
        "  - baz\n"
        "  - qux\n"
        "version: 2.0.0\n"
        "---\n"
        "# Body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert "related:" not in out
    assert "  - bar" not in out
    assert "  - baz" not in out
    assert "  - qux" not in out
    assert "doc_id: foo" in out
    assert "title: Foo" in out
    assert "version: 2.0.0" in out
    assert out.endswith("# Body\n")


def test_strips_audience_inline_list():
    src = (
        "---\n"
        "doc_id: foo\n"
        "audience: [llm, validator, codegen]\n"
        "version: 2.0.0\n"
        "---\n"
        "body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert "audience:" not in out
    assert "doc_id: foo" in out
    assert "version: 2.0.0" in out


def test_preserves_other_fields_with_colons_in_values():
    src = (
        "---\n"
        "doc_id: foo\n"
        'description: "Maps a:b to c:d"\n'
        "related:\n"
        "  - bar\n"
        "version: 2.0.0\n"
        "---\n"
        "body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert 'description: "Maps a:b to c:d"' in out
    assert "related:" not in out
    assert "  - bar" not in out


def test_strips_audience_keeps_related_when_only_audience_in_allowlist():
    """The allowlist behavior is captured by the module-level constant; this test
    documents that BOTH currently-allowlisted fields get stripped together."""
    src = (
        "---\n"
        "doc_id: foo\n"
        "related:\n"
        "  - bar\n"
        "audience: [llm]\n"
        "---\n"
        "body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert "related:" not in out
    assert "audience:" not in out
    assert "doc_id: foo" in out


def test_body_is_byte_identical():
    """Stripping must not touch the body content after the frontmatter close marker."""
    body = (
        "# Title\n"
        "\n"
        "Some text with `code` and **bold** and ---\n"
        "fake frontmatter\n"
        "---\n"
        "more body.\n"
    )
    src = (
        "---\n"
        "doc_id: foo\n"
        "related:\n"
        "  - bar\n"
        "---\n"
        + body
    )
    out = _strip_llm_useless_frontmatter(src)
    assert out.endswith(body)


def test_no_allowlisted_fields_still_yields_valid_frontmatter():
    """When nothing is stripped, the file remains valid YAML frontmatter."""
    src = (
        "---\n"
        "doc_id: foo\n"
        "title: Foo\n"
        "version: 2.0.0\n"
        "---\n"
        "body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    # Output should still parse as a frontmatter doc.
    assert out.startswith("---\n")
    assert "\n---\n" in out[4:]
    assert "doc_id: foo" in out
    assert "title: Foo" in out
    assert "version: 2.0.0" in out


def test_blank_line_between_fields_preserved():
    src = (
        "---\n"
        "doc_id: foo\n"
        "\n"
        "title: Foo\n"
        "related:\n"
        "  - bar\n"
        "version: 2.0.0\n"
        "---\n"
        "body\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert "doc_id: foo" in out
    assert "title: Foo" in out
    assert "related:" not in out


def test_real_doc_06_shape():
    """Sanity check against a frontmatter shape resembling the real Doc 06."""
    src = (
        "---\n"
        "doc_id: equipment-and-inventory-v2\n"
        "title: Equipment and Capability - v2.0.0\n"
        "type: device_model_spec\n"
        "domain: bijective_procedures\n"
        "language: en\n"
        "version: 2.0.0\n"
        "status: current\n"
        "effective_date: 2026-04-28\n"
        "audience: [llm, validator, codegen, operator, integrator]\n"
        "related:\n"
        "  - bijective-procedure-spec-v2\n"
        "  - canonical-text-dsl-v2\n"
        "  - procedure-json-schema-v2\n"
        "  - lifecycle-and-cleanup-v2\n"
        "  - text-json-bijection-v2\n"
        "  - json-code-bijection-v2\n"
        "  - validator-spec-v2\n"
        'checksum: ""\n'
        'description: "..."\n'
        "---\n"
        "# Equipment and Capability - v2.0.0\n"
    )
    out = _strip_llm_useless_frontmatter(src)
    assert "audience:" not in out
    assert "related:" not in out
    for related in (
        "bijective-procedure-spec-v2",
        "canonical-text-dsl-v2",
        "procedure-json-schema-v2",
        "validator-spec-v2",
    ):
        assert related not in out
    # Non-stripped fields stay
    assert "doc_id: equipment-and-inventory-v2" in out
    assert "version: 2.0.0" in out
    assert 'checksum: ""' in out
    assert 'description: "..."' in out
    # Body intact
    assert out.endswith("# Equipment and Capability - v2.0.0\n")
    # Lines saved: 8 (audience + 7-line related block) + 1 (related: header line)
    pre_lines = src.count("\n")
    post_lines = out.count("\n")
    assert pre_lines - post_lines == 9
