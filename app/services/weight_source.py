from abc import ABC, abstractmethod

from ..config import settings


class WeightSource(ABC):
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_weight_kg(self) -> float | None:
        raise NotImplementedError


class ManualWeightSource(WeightSource):
    def is_connected(self) -> bool:
        return True

    def get_weight_kg(self) -> float | None:
        return None


class StubIndicatorWeightSource(WeightSource):
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected

    def get_weight_kg(self) -> float | None:
        return None


def get_indicator_source() -> WeightSource:
    return StubIndicatorWeightSource(settings.indicator_connected)
