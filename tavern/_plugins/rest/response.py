"""
Tavern REST Response Plugin

This module provides the REST response handling and validation for the Tavern testing framework.
It handles HTTP response processing, validation, and verification for REST APIs.

The module contains classes and functions for processing and validating
HTTP responses from REST API endpoints during testing.
"""

import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Union
from urllib.parse import parse_qs, urlparse

import requests
from requests.status_codes import _codes  # type:ignore

from tavern._core import exceptions
from tavern._core.dict_util import deep_dict_merge
from tavern._core.pytest.config import TestConfig
from tavern._core.pytest.newhooks import call_hook
from tavern._core.report import attach_yaml
from tavern.response import BaseResponse, indent_err_text

logger: logging.Logger = logging.getLogger(__name__)


class RestResponse(BaseResponse):
    """REST response implementation for Tavern.

    This class handles the validation and processing of REST HTTP responses.
    It supports status code validation, header checking, and body verification.
    """
    response: requests.Response

    def __init__(
        self, session, name: str, expected, test_block_config: TestConfig
    ) -> None:
        defaults = {"status_code": 200}

        super().__init__(name, deep_dict_merge(defaults, expected), test_block_config)

        def check_code(code: int) -> None:
            if int(code) not in _codes:
                logger.warning("Unexpected status code '%s'", code)

        in_file = self.expected["status_code"]
        try:
            if isinstance(in_file, list):
                for code_ in in_file:
                    check_code(code_)
            else:
                check_code(in_file)
        except TypeError as e:
            raise exceptions.BadSchemaError("Invalid code") from e

    def __str__(self) -> str:
        if self.response:
            return self.response.text.strip()
        else:
            return "<Not run yet>"

    def _verbose_log_response(self, response: requests.Response) -> None:
        """Verbosely log the response object, with query params etc."""

        logger.info("Response: '%s'", response)

        def log_dict_block(block, name):
            if block:
                to_log = name + ":"

                if isinstance(block, list):
                    for v in block:
                        to_log += f"\n  - {v}"
                elif isinstance(block, dict):
                    for k, v in block.items():
                        to_log += f"\n  {k}: {v}"
                else:
                    to_log += f"\n {block}"
                logger.debug(to_log)

        log_dict_block(response.headers, "Headers")

        with contextlib.suppress(ValueError):
            log_dict_block(response.json(), "Body")

        redirect_query_params = self._get_redirect_query_params(response)
        if redirect_query_params:
            parsed_url = urlparse(response.headers["location"])
            to_path = "{}://{}{}".format(*parsed_url)
            logger.debug("Redirect location: %s", to_path)
            log_dict_block(redirect_query_params, "Redirect URL query parameters")

    def _get_redirect_query_params(self, response: requests.Response) -> dict[str, str]:
        """If there was a redirect header, get any query parameters from it"""

        try:
            redirect_url = response.headers["location"]
        except KeyError as e:
            if "redirect_query_params" in self.expected.get("save", {}):
                self._adderr(
                    "Wanted to save %s, but there was no redirect url in response",
                    self.expected["save"]["redirect_query_params"],
                    e=e,
                )
            redirect_query_params = {}
        else:
            parsed = urlparse(redirect_url)
            qp = parsed.query
            redirect_query_params = {i: j[0] for i, j in parse_qs(qp).items()}

        return redirect_query_params

    def _check_status_code(self, status_code: Union[int, list[int]], body: Any) -> None:
        expected_code = self.expected["status_code"]

        if (isinstance(expected_code, int) and status_code == expected_code) or (
            isinstance(expected_code, list) and (status_code in expected_code)
        ):
            logger.debug(
                "Status code '%s' matched expected '%s'", status_code, expected_code
            )
            return
        elif isinstance(status_code, int) and 400 <= status_code < 500:
            # special case if there was a bad request. This assumes that the
            # response would contain some kind of information as to why this
            # request was rejected.
            self._adderr(
                "Status code was %s, expected %s:\n%s",
                status_code,
                expected_code,
                indent_err_text(json.dumps(body)),
            )
        else:
            self._adderr("Status code was %s, expected %s", status_code, expected_code)

    def verify(self, response: requests.Response) -> dict:
        """Verify response against expected values and returns any values that
        we wanted to save for use in future requests

        There are various ways to 'validate' a block - a specific function, just
        matching values, validating a schema, etc...

        Args:
            response: response object

        Returns:
            Any saved values

        Raises:
            TestFailError: Something went wrong with validating the response
        """
        self._verbose_log_response(response)

        call_hook(
            self.test_block_config,
            "pytest_tavern_beta_after_every_response",
            expected=self.expected,
            response=response,
        )

        self.response = response

        # Get things to use from the response
        try:
            body = response.json()
        except ValueError:
            body = None

        redirect_query_params = self._get_redirect_query_params(response)

        # Run validation on response
        self._check_status_code(response.status_code, body)

        self._validate_block("json", body)
        self._validate_block("headers", response.headers)
        self._validate_block("redirect_query_params", redirect_query_params)

        attach_yaml(
            {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body,
                "redirect_query_params": redirect_query_params,
            },
            name="rest_response",
        )

        self._maybe_run_validate_functions(response)

        # Get any keys to save
        saved: dict = {}

        saved.update(self.maybe_get_save_values_from_save_block("json", body))
        saved.update(
            self.maybe_get_save_values_from_save_block("headers", response.headers)
        )
        saved.update(
            self.maybe_get_save_values_from_save_block(
                "redirect_query_params", redirect_query_params
            )
        )

        saved.update(self.maybe_get_save_values_from_ext(response, self.expected))

        # Check cookies
        for cookie in self.expected.get("cookies", []):
            if cookie not in response.cookies:
                self._adderr("No cookie named '%s' in response", cookie)

        if self.errors:
            raise exceptions.TestFailError(
                f"Test '{self.name:s}' failed:\n{self._str_errors():s}",
                failures=self.errors,
            )

        return saved

    def _validate_block(self, blockname: str, block: Mapping) -> None:
        """Validate a block of the response

        Args:
            blockname: which part of the response is being checked
            block: The actual part being checked
        """
        try:
            expected_block = self.expected[blockname]
        except KeyError:
            expected_block = None

        if isinstance(expected_block, dict):
            if expected_block.pop("$ext", None):
                raise exceptions.MisplacedExtBlockException(
                    blockname,
                )

        if blockname == "headers" and expected_block is not None:
            # Special case for headers. These need to be checked in a case
            # insensitive manner
            block = {i.lower(): j for i, j in block.items()}
            expected_block = {i.lower(): j for i, j in expected_block.items()}

        logger.debug("Validating response %s against %s", blockname, expected_block)

        test_strictness = self.test_block_config.strict
        block_strictness = test_strictness.option_for(blockname)
        self.recurse_check_key_match(expected_block, block, blockname, block_strictness)
