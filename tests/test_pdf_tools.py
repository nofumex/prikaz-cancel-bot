from app.services import pdf_tools


def test_find_soffice_returns_path_or_none():
    path = pdf_tools.find_soffice()
    assert path is None or isinstance(path, str)


def test_check_pdf_dependencies_returns_messages():
    ok, errors = pdf_tools.check_pdf_dependencies(require_preview_pdf_for_payment=False)
    assert isinstance(ok, bool)
    assert isinstance(errors, list)
