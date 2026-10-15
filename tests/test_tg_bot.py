from telegrief.tg.bot import next_button, parse_callback, parse_command


def test_parse_callback() -> None:
    assert parse_callback(b"next:12") == 12
    assert parse_callback(b"next:") is None
    assert parse_callback(b"other:1") is None
    assert parse_callback(None) is None


def test_parse_command() -> None:
    assert parse_command("/run Memes 24 0") == ("run", ["Memes", "24", "0"])
    assert parse_command("/run@telegrief_bot Memes") == ("run", ["Memes"])
    assert parse_command("/status") == ("status", [])
    assert parse_command("hello") is None
    assert parse_command("") is None


def test_next_button() -> None:
    assert next_button(None, "Next 10") is None
    button = next_button(5, "Next 10")
    assert button is not None
    assert button.type.data == b"next:5"
    assert button.text == "Next 10"
