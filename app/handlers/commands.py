from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.common import main_menu, profile_menu
from app.models import User
from app.services.cases import latest_case
from app.texts import help_text, profile_text, welcome_text

router = Router(name="commands")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await message.answer(welcome_text(settings.company_name), reply_markup=main_menu())


@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await callback.message.answer(welcome_text(settings.company_name), reply_markup=main_menu())
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text(), reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Текущее действие отменено.", reply_markup=main_menu())


@router.message(Command("profile"))
async def cmd_profile(message: Message, session: AsyncSession, current_user: User) -> None:
    await message.answer(profile_text(current_user, await latest_case(session, current_user.id)), reply_markup=profile_menu())


@router.callback_query(F.data == "profile:show")
async def cb_profile(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    await callback.message.answer(profile_text(current_user, await latest_case(session, current_user.id)), reply_markup=profile_menu())
    await callback.answer()
