"""アカウント残高。quotaの百分率やwindowとは別に保持する。"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class BalanceAmount:
    currency: str
    total_balance: str
    granted_balance: str
    topped_up_balance: str

    def __post_init__(self):
        if self.currency not in {'USD', 'CNY'}:
            raise ValueError('unknown currency')
        for value in (self.total_balance, self.granted_balance, self.topped_up_balance):
            if not isinstance(value, str) or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
                raise ValueError('invalid monetary value')

    def to_dict(self):
        return {name: getattr(self, name) for name in (
            'currency', 'total_balance', 'granted_balance', 'topped_up_balance')}


@dataclass(frozen=True, slots=True)
class AccountBalance:
    is_available: bool
    balance_infos: tuple[BalanceAmount, ...]

    def __post_init__(self):
        if type(self.is_available) is not bool:
            raise ValueError('invalid availability')
        if not isinstance(self.balance_infos, tuple) or not self.balance_infos or not all(
            isinstance(item, BalanceAmount) for item in self.balance_infos
        ):
            raise ValueError('empty or invalid balances')
        if len({item.currency for item in self.balance_infos}) != len(self.balance_infos):
            raise ValueError('duplicate currency')

    @classmethod
    def from_payload(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get('balance_infos'), list) or not all(
            isinstance(item, dict) for item in value['balance_infos']
        ):
            raise ValueError('invalid balance payload')
        return cls(value.get('is_available'), tuple(BalanceAmount(
            **{name: item.get(name) for name in ('currency', 'total_balance',
                                                'granted_balance', 'topped_up_balance')}
        ) for item in value['balance_infos']))

    def to_dict(self):
        return {'is_available': self.is_available,
                'balance_infos': [item.to_dict() for item in self.balance_infos]}

    def to_usage_dict(self):
        usd = next((item for item in self.balance_infos if item.currency == 'USD'), None)
        return {'currency': 'USD', 'total_balance': usd.total_balance if usd else None,
                'is_available': self.is_available}
