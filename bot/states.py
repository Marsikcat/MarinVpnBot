"""FSM-состояния."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    find_user = State()
    grant_days = State()
    broadcast = State()
    plan_field = State()      # ждём новое значение поля тарифа
    plan_new = State()        # ждём «название | дней | цена»
    setting_value = State()   # ждём новое значение настройки
