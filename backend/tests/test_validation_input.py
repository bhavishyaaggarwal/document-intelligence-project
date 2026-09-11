from app.services.document_validation_service import DocumentValidationError, validate_document


def test_unsupported_extension():
    try:
        validate_document("notes.txt", b"hello")
    except DocumentValidationError as exc:
        assert exc.code == "UNSUPPORTED_FILE_TYPE"
    else:
        raise AssertionError("Expected unsupported file error")


def test_empty_file():
    try:
        validate_document("empty.pdf", b"")
    except DocumentValidationError as exc:
        assert exc.code == "EMPTY_FILE"
    else:
        raise AssertionError("Expected empty file error")
