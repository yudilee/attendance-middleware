class AppError(Exception):
    """Base application exception."""
    def __init__(self, code: str, message: str, status_code: int = 400, details: list = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or []

class ValidationError(AppError):
    """Exception raised for model or parameter validation errors."""
    def __init__(self, message: str, details: list = None):
        super().__init__(code="VALIDATION_ERROR", message=message, status_code=400, details=details)

class NotFoundError(AppError):
    """Exception raised when a resource is not found."""
    def __init__(self, message: str, details: list = None):
        super().__init__(code="NOT_FOUND", message=message, status_code=404, details=details)

class AuthError(AppError):
    """Exception raised for authentication or authorization failures."""
    def __init__(self, message: str, details: list = None):
        super().__init__(code="UNAUTHORIZED", message=message, status_code=401, details=details)

class PermissionDeniedError(AppError):
    """Exception raised when an authenticated user lacks permissions."""
    def __init__(self, message: str, details: list = None):
        super().__init__(code="PERMISSION_DENIED", message=message, status_code=403, details=details)
