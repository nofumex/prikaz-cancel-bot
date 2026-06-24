from app.services.name_normalizer import make_short_name, normalize_person_name_from_ocr


def test_genitive_to_nominative_belsky():
    result = normalize_person_name_from_ocr("Бельского Владимира Геннадьевича")
    assert result.normalized == "Бельский Владимир Геннадьевич"


def test_dative_to_nominative_belsky():
    result = normalize_person_name_from_ocr("Бельскому Владимиру Геннадьевичу")
    assert result.normalized == "Бельский Владимир Геннадьевич"


def test_genitive_to_nominative_ivanov():
    result = normalize_person_name_from_ocr("Иванова Ивана Ивановича")
    assert result.normalized == "Иванов Иван Иванович"


def test_dative_to_nominative_ivanov():
    result = normalize_person_name_from_ocr("Иванову Ивану Ивановичу")
    assert result.normalized == "Иванов Иван Иванович"


def test_feminine_petrova():
    result = normalize_person_name_from_ocr("Петровой Анне Сергеевне")
    assert result.normalized == "Петрова Анна Сергеевна"


def test_short_name():
    assert make_short_name("Бельский Владимир Геннадьевич") == "Бельский В.Г."
