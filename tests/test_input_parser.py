import pytest
from autobuild_json.input_parser import parse_accounts
from autobuild_json.errors import FlowError

SECRET = "JBSWY3DPEHPK3PXP"

def test_preserves_password_and_filters():
    result = parse_accounts(f"\ufeffu@example.com| pass:word |{SECRET}\r\nb@example.com|private-pass|\nu@example.com|p|{SECRET}")
    assert result.accounts[0].password == " pass:word "
    assert [r.line_number for r in result.rejected] == [2]
    assert result.duplicate_lines == [3]
    assert "private-pass" not in repr(result.rejected)
    assert " pass:word " not in repr(result.accounts[0])

@pytest.mark.parametrize("row", ["a|b", "a|b|c|d", "|b|c", "a@b.co||ABC", "a@b.co|p|!", "a b@c.co|p|ABC"])
def test_rejects_invalid_rows(row):
    result = parse_accounts(row)
    assert not result.accounts
    assert len(result.rejected) == 1

def test_ignores_blank_not_extra_fields():
    result = parse_accounts(f"\n\n a@b.co|p|{SECRET}\n\n")
    assert len(result.accounts) == 1
    assert result.accounts[0].line_number == 3

@pytest.mark.parametrize("text", ["\n" * 10001, "x" * (5 * 1024 * 1024 + 1)])
def test_limits(text):
    with pytest.raises(FlowError):
        parse_accounts(text)
