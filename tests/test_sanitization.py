"""Unit tests for the URL parameter sanitization helpers."""

import pytest

from paperclip_mcp.server import _issue_ref, _path_param, _uuid_param

UUID = "123e4567-e89b-42d3-a456-426614174000"


class TestPathParam:
    @pytest.mark.parametrize("bad", ["", "   ", "a" * 129, "a\nb", "a\x00b", "a\x7fb"])
    def test_rejects_invalid_values(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _path_param(bad)

    @pytest.mark.parametrize(
        ("raw", "quoted"),
        [
            ("../x", "..%2Fx"),
            ("a/b", "a%2Fb"),
            ("a?x=1", "a%3Fx%3D1"),
            ("a#frag", "a%23frag"),
            ("a&b=c", "a%26b%3Dc"),
        ],
    )
    def test_neutralizes_path_and_query_metacharacters(self, raw: str, quoted: str) -> None:
        assert _path_param(raw) == quoted

    def test_accepts_and_preserves_plain_ids(self) -> None:
        assert _path_param("CY-42") == "CY-42"
        assert _path_param(UUID) == UUID

    def test_strips_whitespace(self) -> None:
        assert _path_param("  CY-42  ") == "CY-42"


class TestIssueRef:
    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "../x", "a/b", "a?x=1", "a#frag", "a.b", "a b", "a" * 65, "ID\n42"],
    )
    def test_rejects_invalid_refs(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _issue_ref(bad)

    @pytest.mark.parametrize("good", ["CY-42", "abc_123", UUID, "a", "A" * 64])
    def test_accepts_valid_refs(self, good: str) -> None:
        assert _issue_ref(good) == good


class TestUuidParam:
    def test_accepts_uuid_and_canonicalizes(self) -> None:
        assert _uuid_param(UUID, "x") == UUID
        assert _uuid_param(UUID.upper(), "x") == UUID
        assert _uuid_param(f"  {UUID}  ", "x") == UUID

    @pytest.mark.parametrize("bad", ["", "CY-42", "../x", "not-a-uuid", UUID + "0"])
    def test_rejects_non_uuids(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _uuid_param(bad, "x")
