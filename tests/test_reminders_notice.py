from types import SimpleNamespace

from app.services.reminders import _inactivity_manager_notice


def test_inactivity_notice_uses_customer_name_instead_of_database_id():
    user = SimpleNamespace(
        id=206,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        platform_user_id="999",
    )

    notice = _inactivity_manager_notice(user)

    assert notice == "Пользователь Анна Иванова бездействует 10 минут. Можно подключиться к чату."
    assert "#206" not in notice


def test_inactivity_notice_falls_back_to_username():
    user = SimpleNamespace(
        id=206,
        first_name=None,
        last_name=None,
        username="anna",
        platform_user_id="999",
    )

    assert "Пользователь anna бездействует" in _inactivity_manager_notice(user)
