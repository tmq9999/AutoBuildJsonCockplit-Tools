import pytest


def test_sse_handles_split_utf8_crlf_and_multiple_data_lines():
    from autobuild_json.gateway.protocols.frames import SSEDecoder
    parser = SSEDecoder(max_frame_bytes=1024)
    wire = '\ufeff: heartbeat\r\nevent: text\r\ndata: {"text":\r\ndata: "chào"}\r\n\r\n'.encode()
    rows = []
    for value in wire:
        rows.extend(parser.feed(bytes([value])))
    assert rows == [("text", '{"text":\n"chào"}')]
    assert parser.finish() == []


def test_sse_size_limit_is_per_frame_and_truncation_is_not_success():
    from autobuild_json.gateway.protocols.frames import SSEDecoder
    parser = SSEDecoder(max_frame_bytes=32)
    assert len(parser.feed(b"data: x\n\n"*100)) == 100
    with pytest.raises(ValueError):
        parser.feed(b"data: "+b"x"*40)
    parser = SSEDecoder()
    parser.feed(b"data: partial")
    with pytest.raises(ValueError):
        parser.finish()


def test_ndjson_handles_split_records_and_invalid_utf8():
    from autobuild_json.gateway.protocols.frames import NDJSONDecoder
    parser = NDJSONDecoder()
    assert parser.feed(b'{"a":') == []
    assert parser.feed(b'1}\n{"b":2}\n') == [{"a": 1}, {"b": 2}]
    assert parser.finish() == []
    with pytest.raises(ValueError):
        parser.feed(b'{"text":"\xff"}\n')


def test_sse_comments_are_not_empty_data_events():
    from autobuild_json.gateway.protocols.frames import SSEDecoder
    parser = SSEDecoder()
    assert parser.feed(b": hi\n\nevent: ignored\n\n") == []
