from telegrief.tg.protocols import BotGateway, UserGateway
from tests.fakes import FakeBot, FakeUser


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeUser(), UserGateway)
    assert isinstance(FakeBot(), BotGateway)
