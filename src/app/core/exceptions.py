from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for domain errors that map to a specific HTTP status."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    default_message = "Request could not be processed"

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    default_message = "Resource not found"


class SlotUnavailableError(AppError):
    status_code = status.HTTP_409_CONFLICT
    default_message = "Requested time slot is not available"


class DuplicateBookingError(AppError):
    status_code = status.HTTP_409_CONFLICT
    default_message = "An active appointment already exists for this slot"


class OutsideBusinessHoursError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_message = "Requested time is outside business working hours/days"


class InvalidCredentialsError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_message = "Invalid email or password"


class EdesyIntegrationError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_message = "Edesy voice agent API request failed"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
