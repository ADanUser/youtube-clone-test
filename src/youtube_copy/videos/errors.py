"""Safe application errors; never expose storage or database diagnostics."""


class UploadError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)
