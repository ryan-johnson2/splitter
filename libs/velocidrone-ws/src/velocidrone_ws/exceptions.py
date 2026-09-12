"""Custom exceptions for the VelociDrone WebSocket client."""


class VelociDroneWSError(Exception):
    """Base exception for VelociDrone WebSocket errors."""


class ConnectionError(VelociDroneWSError):
    """Raised when a WebSocket connection fails."""


class CommandError(VelociDroneWSError):
    """Raised when sending a command fails."""
