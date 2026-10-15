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


class _FakeClient:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add_event_handler(self, callback: object, event: object) -> None:
        self.handlers.append(callback)


class _FakeEvent:
    def __init__(self, data: bytes, sender_id: int) -> None:
        self.data = data
        self.sender_id = sender_id
        self.answers: list[str | None] = []

    async def answer(self, message: str | None = None) -> None:
        self.answers.append(message)


async def test_next_button_ignores_non_owner() -> None:
    from telegrief.tg.bot import TelethonBot

    client = _FakeClient()
    bot = TelethonBot(client, "telegrief_bot", owner_id=1)  # type: ignore[arg-type]
    pressed: list[int] = []

    async def handler(digest_id: int) -> None:
        pressed.append(digest_id)

    bot.on_next_button(handler)
    callback = client.handlers[0]
    stranger = _FakeEvent(b"next:5", sender_id=999)
    await callback(stranger)  # type: ignore[operator]
    assert pressed == []
    owner = _FakeEvent(b"next:5", sender_id=1)
    await callback(owner)  # type: ignore[operator]
    assert pressed == [5]
