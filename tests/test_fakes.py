from telegrief.tg.protocols import BotGateway, UserGateway
from tests.fakes import FakeBot, FakeUser


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeUser(), UserGateway)
    assert isinstance(FakeBot(), BotGateway)


async def test_fake_bot_records_menu() -> None:
    bot = FakeBot()
    await bot.set_menu([("help", "this list")], "About text")
    assert bot.menu == [("help", "this list")]
    assert bot.about == "About text"
