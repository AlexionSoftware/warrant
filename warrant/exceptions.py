class WarrantException(Exception):
    """Base class for all Warrant exceptions"""


class ForceChangePasswordException(WarrantException):
    """Raised when the user is forced to change their password"""


class MFATokenRequiredException(WarrantException):
    """Raised when there is a token required to authenticate"""


class TokenVerificationException(WarrantException):
    """Raised when token verification fails."""
