"""
Tavern Request Module

This module provides the core request functionality for the Tavern testing framework.
It handles HTTP request creation, formatting, and execution for API testing.

The module contains classes and functions for building and sending HTTP requests
with various authentication methods, headers, and payload formats.
"""

import logging
from abc import abstractmethod
from typing import Any

import box

from tavern._core.pytest.config import TestConfig

logger: logging.Logger = logging.getLogger(__name__)


class BaseRequest:
    """Base class for all request types in Tavern.

    This abstract base class defines the interface that all request
    implementations must follow. It provides methods for request execution
    and variable management.
    """
    @abstractmethod
    def __init__(
        self, session: Any, rspec: dict, test_block_config: TestConfig
    ) -> None: ...

    @property
    @abstractmethod
    def request_vars(self) -> box.Box:
        """
        Variables used in the request

        What is contained in the return value will change depending on the type of request

        Returns:
            box.Box: box of request vars
        """

    @abstractmethod
    def run(self):
        """Run test"""
