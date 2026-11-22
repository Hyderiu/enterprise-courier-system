from abc import ABC, abstractmethod


class CourierClient(ABC):
    name = ""

    @abstractmethod
    def create_order(self, order_data: dict) -> dict:
        pass

    @abstractmethod
    def query_fee(self, sender: dict, receiver: dict, weight: float) -> dict:
        pass

    @abstractmethod
    def query_track(self, courier_order_no: str) -> list:
        pass

    @abstractmethod
    def cancel_order(self, courier_order_no: str, order_no: str = "") -> dict:
        pass

    @property
    def is_mock(self) -> bool:
        return False
